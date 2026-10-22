#!/usr/bin/env python3
"""Проверка планирования прогона: окна не кончаются на первой порции.

    python tests/test_plan_round.py

Сеть не нужна: VK подменяется заглушками, база и файлы — во временной папке.
Проверяем, что «период 30 суток и несколько тегов» собирается одним запуском:
порция (воркеров × windows_per_worker) держит очередь полной, теги идут по кругу
(двигаются все сразу, а не первый по списку), прогон продолжается, пока период
не пройден целиком, а следующий «Старт» уже собранное не переделывает. Отдельно —
«Работать, ч»: круг без новых постов ждёт «Пауза, с», а не крутит тот же пустой
хвост подряд (иначе каждый круг это запрос к VK, лестница и окно-близнец в базе).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import run_tags  # noqa: E402
from accounts import load_accounts  # noqa: E402
from settings import load_settings  # noqa: E402
from state_db import DB  # noqa: E402

TAGS = ("маникюр", "педикюр", "наращивание", "дизайн")
PHONES = ("+79990000001", "+79990000002", "+79990000003")
PERIOD_HOURS = 720.0
WINDOW_HOURS = 6.0
WORKERS = 3
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


tmp = Path(tempfile.mkdtemp(prefix="plan_round_"))
accounts_file = tmp / "accounts.txt"
accounts_file.write_text(
    "\n".join(f"{phone};пароль;;{i}.{i}.{i}.{i}:1111:user:pass" for i, phone in enumerate(PHONES, 1)) + "\n",
    encoding="utf-8",
)
accounts = load_accounts(accounts_file)
db = DB(tmp / "parser.sqlite3")
settings = load_settings()
settings["window_hours"] = WINDOW_HOURS
settings["windows_per_worker"] = 3

# VK не нужен: окно «собирается» мгновенно, зато видно, какие окна запрошены.
taken: list[tuple[str, int, int]] = []
logs: list[str] = []


def fake_make_client(session_path, proxy, pause, label):
    return type("Client", (), {"login": label, "token": "t", "api_version": "5.199"})()


def fake_run_window(client, db_, tag, **kw):
    taken.append((tag, int(kw["start_time"]), int(kw["end_time"])))
    return {"status": "ok", "reason": "done", "seen": 10, "new": 4, "pages": 1,
            "need_split": False, "cursor": ""}


run_tags.make_client = fake_make_client
run_tags.probe_mod.run_window = fake_run_window
run_tags.maybe_live_export = lambda *a, **k: None
run_tags.log = logs.append  # сотни окон: лог прогона собираем, но не печатаем

args = run_tags.parse_args(["--hours", f"{PERIOD_HOURS:g}", "--workers", str(WORKERS)])
run_tags.resolve_period(args, settings)
args.end_time = int(args.start_time) + int(PERIOD_HOURS * 3600)  # конец периода фиксируем
pool, workers = run_tags.build_pool(accounts, db, WORKERS, "")
check(workers == WORKERS, f"в работе все аккаунты: воркеров {workers}")

# 1. порция — буфер очереди, а весь период лежит в плане
planner = run_tags.RoundPlanner(db, TAGS, args, settings, 1)
span = args.end_time - int(args.start_time)
per_tag = span // int(WINDOW_HOURS * 3600)
need = len(TAGS) * per_tag
check(planner.total_windows == need, f"в плане весь период: окон {planner.total_windows} из {need}")
check(
    planner.total_seconds == span,
    "знаменатель «периода» — запрошенный отрезок, а не сумма длин окон: "
    f"{planner.total_seconds / 3600:.1f} ч",
)
check(
    planner.progress_text() == f"окон 0/{need}, период 0.0 ч из {PERIOD_HOURS:.1f} ч (0%)",
    f"пустой план в начале прогона: {planner.progress_text()}",
)
check(
    planner.coverage_share() == 0.0,
    "пустая база: охват периода 0 — прогон начинает с нуля",
)
first = planner.batch()
batch_size = WORKERS * int(settings["windows_per_worker"])
check(len(first) == batch_size, f"первая порция = воркеров × окон на поток: {len(first)}")
check(
    {j["tag"] for j in first} == set(TAGS),
    f"в первой порции все теги, а не только первый: {sorted({j['tag'] for j in first})}",
)
check(planner.left_windows() == need - len(first), f"остаток плана ждёт очереди: {planner.left_windows()}")
check(
    int(first[0]["end_time"]) >= max(int(j["end_time"]) for j in first),
    "окна идут от свежих к старым",
)

# 2. прогон идёт, пока период не пройден целиком (так же, как в main)
batches, stuck, jobs = 0, 0, first
while jobs:
    batches += 1
    if run_tags.run_round(jobs, pool, workers, args, settings, db):
        stuck += 1
    jobs = planner.batch()
check(stuck == 0, f"каждую порцию добивают целиком (застряло {stuck})")
check(batches > 1, f"запуск не закончился на первой порции: порций {batches}")
check(planner.left_windows() == 0, f"план пройден до конца: ждёт очереди {planner.left_windows()}")
check(planner.progress_text().endswith("(100%)"), f"прогресс дошёл до 100%: {planner.progress_text()}")
check(
    f"период {PERIOD_HOURS:.1f} ч из {PERIOD_HOURS:.1f} ч" in planner.progress_text(),
    f"к концу прогона охвачен ровно период: {planner.progress_text()}",
)

done = int(db.con.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'done'").fetchone()["n"])
check(done == need, f"закрыты все окна периода: {done} из {need}")
counted = {tag: sum(1 for t, _, _ in taken if t == tag) for tag in TAGS}
check(set(counted.values()) == {per_tag}, f"каждый тег прошёл весь период окнами: {counted}")
hours: dict[str, float] = {}
for tag, lo, hi in taken:
    hours[tag] = hours.get(tag, 0) + (hi - lo)
check(
    set(hours.values()) == {span},
    "окна каждого тега покрывают период без дыр и нахлёстов",
)
check(min(lo for _, lo, _ in taken) == int(args.start_time), "самое старое окно — с начала периода")
check(db.count_open_jobs(TAGS[0]) == 0, "недобитых окон в базе нет")

# 3. повторный «Старт»: свежий хвост периода перепроверяем (лента меняется), старое — нет
tail_hours = float(settings["refresh_tail_hours"])
tail_lo = int(args.end_time) - int(tail_hours * 3600)
again = run_tags.RoundPlanner(db, TAGS, args, settings, 1)
check(
    again.total_windows == len(TAGS),
    f"второй запуск берёт только хвост {tail_hours:g} ч: окон {again.total_windows}",
)
check(
    again.progress_text()
    == f"окон 0/{len(TAGS)}, период {PERIOD_HOURS:.1f} ч из {PERIOD_HOURS:.1f} ч (100%)",
    f"период уже в базе — перепроверяем только хвост: {again.progress_text()}",
)
# По охвату до раздачи окон прогон и понимает, что период был собран ещё до «Старт»:
# в конце он пишет об этом и подсказывает «Работать, ч» (--run-hours), иначе «период
# 6 часов, а работа на минуту» выглядит как поломка.
check(
    again.coverage_share() >= 0.999,
    "период уже в базе: охват 100% — прогон сводится к перепроверке хвоста",
)
_run_src = Path(run_tags.__file__).read_text(encoding="utf-8")
check(
    "covered_before = planner.coverage_share()" in _run_src
    and "период уже был собран целиком" in _run_src
    and "«Работать, ч»" in _run_src,
    "и в конце прогона есть подсказка «Работать, ч» на случай «период собран до запуска»",
)
tail_jobs = again.batch()
check(
    {(j["tag"], int(j["start_time"]), int(j["end_time"])) for j in tail_jobs}
    == {(tag, tail_lo, int(args.end_time)) for tag in TAGS},
    "перепроверяются ровно собранные окна хвоста, границы те же: "
    + str([(j["tag"], int(j["start_time"]), int(j["end_time"])) for j in tail_jobs]),
)
check(
    len(tail_jobs) == len(TAGS) and tail_jobs[0]["status"] == "done",
    "окно хвоста берётся из базы тем же (ни новых строк, ни дробления)",
)

# 3.2. --no-refresh: хвост не перепроверяем — собирать действительно нечего
settings["refresh_tail_hours"] = 0
quiet = run_tags.RoundPlanner(db, TAGS, args, settings, 1)
check(quiet.total_windows == 0, f"без перепроверки хвоста собирать нечего (окон {quiet.total_windows})")
check(quiet.batch() == [], "второй запуск не ставит окон в очередь")
check(
    quiet.progress_text()
    == f"окон 0/0, период {PERIOD_HOURS:.1f} ч из {PERIOD_HOURS:.1f} ч (100%)",
    f"пустой план: {quiet.progress_text()}",
)
settings["refresh_tail_hours"] = tail_hours

# 4. недобитые окна прошлого запуска: добираются из очереди, а не планируются заново
db2 = DB(tmp / "resume.sqlite3")
newest = (int(args.end_time) - int(WINDOW_HOURS * 3600), int(args.end_time))
pending = db2.add_job(TAGS[0], newest[0], newest[1], 1)  # самое свежее окно первого тега
open_jobs = db2.open_jobs(TAGS)
check(len(open_jobs) == 1, f"недобитое окно прошлого запуска видно как pending: {len(open_jobs)}")
resume_planner = run_tags.RoundPlanner(db2, TAGS, args, settings, 1, resume=open_jobs)
check(
    resume_planner.total_windows == need - 1,
    f"оно не попало в новый план: окон {resume_planner.total_windows} из {need - 1}",
)
planned: list[dict] = []
while True:
    chunk = resume_planner.batch()
    if not chunk:
        break
    planned.extend(chunk)
bounds = {(j["tag"], int(j["start_time"]), int(j["end_time"])) for j in planned}
check(
    (TAGS[0], int(pending["start_time"]), int(pending["end_time"])) not in bounds,
    "то же окно второй раз не планируется — его уже добирают из очереди",
)
check(len(planned) == need - 1, f"остальные окна периода на месте: {len(planned)}")
check(
    db2.count_open_jobs(TAGS[0]) == per_tag,
    f"в очереди тега окна всего периода (с недобитым): {db2.count_open_jobs(TAGS[0])}",
)
check(db2.drop_open_jobs(TAGS) == need, f"брошенные окна снимаются с очереди (--no-resume): {need}")

# 5. остатки прошлого запуска вне периода не добираются: «за 6 часов» — только 6 часов
db3 = DB(tmp / "stale.sqlite3")
old_lo = int(args.start_time) - 11 * 86400  # окно давнего (прерванного) прогона
stale = db3.add_job(TAGS[0], old_lo, old_lo + 3600, 1)
near_lo = int(args.end_time) - 3600         # окно внутри текущего периода
inside = db3.add_job(TAGS[0], near_lo, int(args.end_time), 1)
pending = db3.open_jobs(TAGS)
check(len(pending) == 2, f"в базе оба остатка видны как pending: {len(pending)}")
kept = run_tags.resume_windows(db3, pending, args, settings)
check(
    [int(j["id"]) for j in kept] == [int(inside["id"])],
    "в работу берём только окно внутри периода",
)
check(db3.count_open_jobs(TAGS[0]) == 1, "устаревшее окно снято с очереди")
row = db3.con.execute("SELECT status, reason FROM jobs WHERE id = ?", (int(stale["id"]),)).fetchone()
check(
    row["status"] == "dropped" and row["reason"] == "out_of_period",
    f"устаревшее окно помечено dropped: {row['status']}/{row['reason']}",
)
left_after = db3.open_jobs(TAGS)
check(
    [int(j["id"]) for j in left_after] == [int(inside["id"])],
    "следующий запуск видит только живое окно",
)

# 5.1. остатки, чьи часы уже собраны другими окнами, не добираем: по ним лента
# отдаёт то же самое — в логе это «постов N, новых 0», а непройденные часы ждут
db7 = DB(tmp / "covered.sqlite3")
col_lo, col_hi = int(args.end_time) - 6 * 3600, int(args.end_time) - 4 * 3600
collected = db7.add_job(TAGS[0], col_lo, col_hi, 1)  # часы уже в базе (done)
db7.update_job(collected["id"], status="done", seen=10, new=10, pages=1, reason="end_of_feed")
redundant = db7.add_job(TAGS[0], col_lo + 1800, col_hi, 1)      # целиком внутри собранного
partial = db7.add_job(TAGS[0], col_lo - 3600, col_lo + 1800, 1)  # часть часов не собрана
pending = db7.open_jobs(TAGS)
check(len(pending) == 2, f"в базе два остатка прошлого запуска: {len(pending)}")
kept = run_tags.resume_windows(db7, pending, args, settings)
check(
    [int(j["id"]) for j in kept] == [int(partial["id"])],
    "добираем только окно, где есть несобранные часы",
)
row = db7.con.execute(
    "SELECT status, reason FROM jobs WHERE id = ?", (int(redundant["id"]),)
).fetchone()
check(
    row["status"] == "dropped" and row["reason"] == "already_covered",
    f"окно внутри собранного снято как перепроверка впустую: {row['status']}/{row['reason']}",
)
check(
    db7.count_open_jobs(TAGS[0]) == 1,
    f"в очереди остался один остаток: {db7.count_open_jobs(TAGS[0])}",
)

# 5.2. та же проверка на реальном прогоне: прошлый прогон оставил окно целиком
# внутри собранного — оно не должно съедать запросы
db8 = DB(tmp / "covered_run.sqlite3")
span_lo, span_hi = int(args.end_time) - 12 * 3600, int(args.end_time) - 6 * 3600
whole = db8.add_job(TAGS[0], span_lo, span_hi, 1)
db8.update_job(whole["id"], status="done", seen=10, new=10, pages=1, reason="end_of_feed")
for i in range(4):  # остатки прошлого запуска внутри собранных часов
    part = db8.add_job(TAGS[0], span_lo + i * 1800, span_lo + (i + 1) * 1800, 1)
    db8.update_job(part["id"], seen=5, new=0)
left_before = db8.count_open_jobs(TAGS[0])
kept_run = run_tags.resume_windows(db8, db8.open_jobs(TAGS), args, settings)
check(left_before == 4 and kept_run == [], "остатки внутри собранного не идут в работу")
check(db8.count_open_jobs(TAGS[0]) == 0, "и в очереди их не осталось")

# 6. перепроверка окна не копит счётчики: окно остаётся «собранным»
db4 = DB(tmp / "rewalk.sqlite3")
re_lo, re_hi = int(args.end_time) - 1800, int(args.end_time)
job = db4.add_job(TAGS[0], re_lo, re_hi, 1)
db4.update_job(job["id"], status="done", seen=12, new=12, pages=1, reason="end_of_feed")
job = dict(db4.con.execute("SELECT * FROM jobs WHERE id = ?", (int(job["id"]),)).fetchone())
res_walk = {"status": "ok", "reason": "end_of_feed", "seen": 12, "new": 2, "pages": 1,
            "need_split": False, "cursor": ""}
run_tags.handle_result(db4, job, res_walk, run_tags.JobQueue(), settings, "acc1")
row = db4.con.execute("SELECT status, seen, new FROM jobs WHERE id = ?", (int(job["id"]),)).fetchone()
check(
    row["status"] == "done" and row["seen"] == 12 and row["new"] == 2,
    f"повтор окна берёт счётчики заново: seen={row['seen']}, new={row['new']}",
)
check(
    db4.covered_intervals(TAGS[0], int(settings["split_if_seen"])) == [(re_lo, re_hi)],
    "окно после перепроверки по-прежнему считается собранным",
)
# а недобитое окно (повтор после сбоя) счётчики по-прежнему накапливает
job2 = db4.add_job(TAGS[0], re_lo - 7200, re_lo - 3600, 1)
db4.update_job(job2["id"], seen=4, new=4)
res_part = {**res_walk, "seen": 5, "new": 1}
job2 = dict(db4.con.execute("SELECT * FROM jobs WHERE id = ?", (int(job2["id"]),)).fetchone())
run_tags.handle_result(db4, job2, res_part, run_tags.JobQueue(), settings, "acc1")
row2 = db4.con.execute("SELECT seen, new FROM jobs WHERE id = ?", (int(job2["id"]),)).fetchone()
check(
    row2["seen"] == 9 and row2["new"] == 5,
    f"повтор после сбоя копит счётчики: seen={row2['seen']}, new={row2['new']}",
)

# 7. перепроверяется только хвост: окно старше хвоста остаётся закрытым
db5 = DB(tmp / "tail.sqlite3")
args5 = run_tags.parse_args(["--workers", str(WORKERS)])
args5.start_time = int(args.end_time) - 12 * 3600
args5.end_time = int(args.end_time)
mid = int(args.end_time) - 6 * 3600
fresh = db5.add_job(TAGS[0], mid, int(args.end_time), 1)          # свежее окно хвоста
older = db5.add_job(TAGS[0], int(args5.start_time), mid, 1)       # окно старше хвоста
for done_job in (fresh, older):
    db5.update_job(done_job["id"], status="done", seen=10, new=10, pages=1, reason="end_of_feed")
tail_planner = run_tags.RoundPlanner(db5, [TAGS[0]], args5, settings, 1)
check(tail_planner.total_windows == 1, f"в хвосте 6 ч одно собранное окно: {tail_planner.total_windows}")
planned_tail = tail_planner.batch()
check(
    [(int(j["start_time"]), int(j["end_time"])) for j in planned_tail] == [(mid, int(args.end_time))],
    "перепроверяем свежее окно, старое не трогаем",
)
settings["refresh_tail_hours"] = 12
whole = run_tags.RoundPlanner(db5, [TAGS[0]], args5, settings, 1)
check(whole.total_windows == 2, f"хвост во весь период — перепроверяем оба окна: {whole.total_windows}")
settings["refresh_tail_hours"] = 0
closed = run_tags.RoundPlanner(db5, [TAGS[0]], args5, settings, 1)
check(closed.total_windows == 0, f"без перепроверки период закрыт целиком: {closed.total_windows}")
settings["refresh_tail_hours"] = tail_hours
check(
    run_tags.parse_args(["--refresh-hours", "2"]).refresh_hours == 2.0
    and run_tags.parse_args(["--no-refresh"]).no_refresh is True,
    "--refresh-hours и --no-refresh понимаются",
)

# 8. перепроверка не выходит за период и не дробит уже закрытое окно
wide = {
    "tag": TAGS[0], "round": 1, "status": "done",
    "start_time": int(args.start_time) - 3600, "end_time": int(args.start_time) + 3600,
}
check(
    run_tags.fetch_bounds(wide, args) == (int(args.start_time), wide["end_time"]),
    f"перепроверка обрезается началом периода: {run_tags.fetch_bounds(wide, args)}",
)
tail_lo = int(args.end_time) - 1800
late = {**wide, "start_time": tail_lo, "end_time": int(args.end_time) + 2 * 3600}
check(
    run_tags.fetch_bounds(late, args) == (tail_lo, int(args.end_time)),
    f"и концом периода: {run_tags.fetch_bounds(late, args)}",
)
check(
    run_tags.fetch_bounds({**wide, "start_time": tail_lo, "end_time": int(args.end_time)}, args)
    == (tail_lo, int(args.end_time)),
    "окно внутри периода качается как записано",
)
check(
    run_tags.fetch_bounds({**wide, "status": "pending"}, args)
    == (wide["start_time"], wide["end_time"]),
    "недобитое окно берём целиком: кусок не должен остаться «собранным»",
)

# 9. осколок периода (десяток секунд) не становится отдельным окном
t0 = 1_700_000_000
SIX_H = 6 * 3600
check(
    run_tags.split_windows(t0, t0 + SIX_H + 14, SIX_H) == [(t0, t0 + SIX_H + 14)],
    f"14 секунд приращиваются к окну: {run_tags.split_windows(t0, t0 + SIX_H + 14, SIX_H)}",
)
two = run_tags.split_windows(t0, t0 + 12 * 3600 + 1, SIX_H)
check(
    len(two) == 2 and two[-1] == (t0, t0 + SIX_H + 1),
    f"секунда сверх двух окон достаётся старому окну, а не третьему: {two}",
)
odd = run_tags.split_windows(t0, t0 + 30 * 3600 + 7, SIX_H)
check(
    all(b - a >= 60 for a, b in odd) and len(odd) >= 5,
    f"окон короче минуты не бывает: {[b - a for a, b in odd]}",
)
check(
    sum(b - a for a, b in odd) == 30 * 3600 + 7 and odd[-1][0] == t0,
    "окна по-прежнему без дыр и нахлёстов покрывают период",
)
odd_args = run_tags.parse_args(["--workers", str(WORKERS)])
odd_args.start_time = t0
odd_args.end_time = t0 + SIX_H + 14  # «за 6 часов» почти всегда длиннее ровно 6 часов
odd_planner = run_tags.RoundPlanner(DB(tmp / "frag.sqlite3"), [TAGS[0]], odd_args, settings, 1)
planned_odd = [(int(j["start_time"]), int(j["end_time"])) for j in odd_planner.batch()]
check(
    planned_odd == [(t0, t0 + SIX_H + 14)],
    f"план не плодит окно на 14 секунд: {planned_odd}",
)

db6 = DB(tmp / "clamp.sqlite3")
cut_lo, cut_hi = int(args.start_time) - 1800, int(args.start_time) + 1800
old_job = db6.add_job(TAGS[0], cut_lo, cut_hi, 1)
db6.update_job(old_job["id"], status="done", seen=5, new=5, pages=1, reason="end_of_feed")
old_job = dict(db6.con.execute("SELECT * FROM jobs WHERE id = ?", (int(old_job["id"]),)).fetchone())
queue = run_tags.JobQueue()
run_tags.handle_result(
    db6, old_job, {**res_walk, "seen": 40, "new": 40, "need_split": True}, queue, settings, "acc1"
)
n_rows = int(db6.con.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])
check(
    n_rows == 1 and queue.get() is None,
    f"перепроверка закрытого окна не дробится: строк в базе {n_rows}",
)
check(
    db6.con.execute(
        "SELECT status FROM jobs WHERE id = ?", (int(old_job["id"]),)
    ).fetchone()["status"] == "done",
    "окно после перепроверки остаётся закрытым",
)
check(
    run_tags.fetch_bounds(old_job, args) == (int(args.start_time), cut_hi),
    f"запрос по нему идёт только с начала периода: {run_tags.fetch_bounds(old_job, args)}",
)

# 10. «Работать, ч» (--run-hours): период — это ЧТО собирать, а работать — сколько задано.
# Один проход кончается, как только период охвачен (поэтому «за 6 часов» на уже
# собранных часах — минута работы), а «Работать, ч» держит процесс столько часов.
check(
    run_tags.parse_args(["--workers", str(WORKERS)]).run_hours == 0.0
    and run_tags.parse_args(["--run-hours", "6"]).run_hours == 6.0,
    "--run-hours понимается (0 — один проход, как раньше)",
)
check(
    abs(run_tags.duty_left(1000.0, 6, now=1000.0) - 6 * 3600) < 0.01
    and run_tags.duty_left(1000.0, 6, now=1000.0 + 6 * 3600) <= 0
    and run_tags.duty_left(1000.0, 0.0, now=1000.0) == 0.0,
    "работа отсчитывается от старта процесса и уходит в ноль по сроку",
)
check(
    run_tags.fmt_left(3661).startswith("1 ч 1 мин"),
    f"остаток времени читается по-русски: {run_tags.fmt_left(3661)}",
)

# 11. «Работать, ч»: круг, который не принёс ни одного нового поста, ждёт «Пауза, с».
# Раньше ветка «новых постов нет» была недостижимой: свежий хвост периода всегда даёт
# окно, поэтому прогон крутил его каждые пару секунд — на каждый круг запрос к VK,
# лестница (F5, другой count, дробление) и новая строка-близнец окна в базе (в живом
# прогоне так набралось 32 pending-окна за 66 секунд, и все они ждали следующий «Старт»).
run_tags._stats["new"] = 0
check(
    run_tags.duty_round_waits(6, 0) and run_tags.duty_round_waits(0.5, 12),
    "круг без новых постов уходит в «Пауза, с», а не повторяется сразу",
)
run_tags._stats["new"] = 7
check(
    not run_tags.duty_round_waits(6, 0) and run_tags.duty_round_waits(6, 7),
    "пришли посты — новый круг начинается сразу, пауза только за пустой круг",
)
run_tags._stats["new"] = 0
check(
    not run_tags.duty_round_waits(0, 0) and not run_tags.duty_round_waits(0, 5),
    "без «Работать, ч» поведение прежнее: один проход и никаких пауз",
)
check(
    "duty_round_waits(duty_hours, round_new_mark)" in _run_src
    and "новых постов нет — проверю снова" in _run_src,
    "в цикле «Работать, ч» пауза стоит после круга без новых постов",
)

# 11.2. Прогон целиком: период собран, VK отдаёт пустоту — кругов мало, и каждый
# ждёт «Пауза, с». Без паузы за те же секунды прогон успел бы выжечь десятки окон.
duty_tags = tmp / "tags_duty.txt"
duty_tags.write_text("маникюр\n", encoding="utf-8")
duty_logs: list[str] = []
saved = (run_tags.log, run_tags.probe_mod.run_window, run_tags.DB, run_tags.ACCOUNTS_FILE)


def empty_window(client, db_, tag, **kw):
    """VK отвечает пусто: по свежему хвосту постов нет (индекс VK их ещё не отдал)."""
    taken.append((tag, int(kw["start_time"]), int(kw["end_time"])))
    return {"status": "ok", "reason": "end_of_feed", "seen": 0, "new": 0, "pages": 1,
            "need_split": False, "cursor": ""}


run_tags.DB = lambda **kw: DB(tmp / "duty.sqlite3", **kw)  # не трогаем data/parser.sqlite3
run_tags.ACCOUNTS_FILE = accounts_file
run_tags.log = duty_logs.append
run_tags.probe_mod.run_window = empty_window
taken.clear()
try:
    duty_code = run_tags.main([
        str(duty_tags), "--hours", "6", "--workers", "1", "--run-hours", "0.0008",
        "--repeat-pause", "1", "--no-browser", "--no-session-refresh",
        "--no-export", "--no-validate",
    ])
finally:
    run_tags.log, run_tags.probe_mod.run_window, run_tags.DB, run_tags.ACCOUNTS_FILE = saved

check(
    duty_code == 0 and any("новых постов нет — проверю снова" in line for line in duty_logs),
    "на собранном периоде прогон ждёт паузу и говорит «новых постов нет — проверю снова»",
)
check(
    len(taken) <= 5,
    f"пустой хвост не гоняется подряд: за время работы запросов к VK {len(taken)}",
)

shutil.rmtree(tmp, ignore_errors=True)
print(f"(лог прогона: {len(logs)} строк)")
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ПЛАН ПРОГОНА РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)
