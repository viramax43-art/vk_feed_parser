#!/usr/bin/env python3
"""Проверка пустого окна в прогоне: «тихий ноль» не превращается в лесенку пауз.

    python tests/test_empty_window.py

Сеть и браузер не нужны: VK подменяется заглушками, база — во временной папке.
Проверяем то, из-за чего прогон стоял на одном окне:

  0) «первый пост в базе» — не предел выдачи VK: старое окно (раньше первого поста
     тега) прогон спрашивает у VK и собирает. Раньше такие окна закрывались без
     запроса (`out_of_period`), и участок 18.09–12.10 у «бровиста» выпал из сбора,
     хотя VK отдаёт 05.10 по 45 постов на окно. Пустое окно, по которому в базе ещё
     нет постов, повторяется в конце очереди (через круг тихое ограничение VK обычно
     отпускает), а не закрывается как «собрано» с первого пустого ответа;
  1) окно, на которое VK молча отвечает пустой выдачей, перезапрашивается ДРУГИМ
     запросом (меньший count), а не паузами: паузы 15/45/90/150/300 с ничего не
     меняли, потому что запрос оставался тем же;
  2) пустое окно не задерживает работу: соседние окна докачиваются и закрываются, а
     само окно остаётся в базе pending (status = done ему не ставится) — его добьёт
     следующий «Старт» или другой аккаунт, окна не теряются;
  3) аккаунт не уходит в отлёжку из-за одного окна — для отлёжки нужно пусто на
     нескольких РАЗНЫХ окнах;
  4) «access_token has expired» после обновления сессии не крутит окно по F5
     бесконечно: после EMPTY_TOKEN_REFRESH_RETRIES обновлений аккаунт выбывает с
     понятной причиной, а окно ждёт живого аккаунта в очереди и в базе;
  5) лестница (F5 → другой count → дробление) идёт один раз на окно и один раз на
     аккаунт за прогон: когда пустых окон подряд много, аккаунт переходит в «тихий»
     режим и берёт каждое окно ОДНИМ запросом — прогон не молотит те же пустые
     выдачи и не открывает браузер на каждое окно;
  6) когда VK молча режет выдачу СРАЗУ ВСЕМ аккаунтам, раунд прерывается одной
     строкой в логе (а не выжигает очередь сотнями пустых запросов и строк):
     недобитые окна остаются pending, их добьёт следующий заход.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import run_tags  # noqa: E402
import vk_auth  # noqa: E402
from settings import load_settings  # noqa: E402
from state_db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def write_session(session_path: Path, token: str) -> dict:
    """Пишет файл сессии так, как это делает vk_auth."""
    payload = {
        "access_token": token,
        "token_expires_at": int(time.time()) + 86400,
        "api_version": "5.289",
        "login": Path(session_path).stem.replace("session_", ""),
    }
    Path(session_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


class FakePool:
    """Пул-заглушка: воркеру нужен только add_posts."""

    def __init__(self) -> None:
        self.posts = 0

    def add_posts(self, acc, count) -> None:
        self.posts += int(count or 0)


class FakeClient:
    """Клиент аккаунта: воркеру нужны токен и версия API."""

    def __init__(self, login: str) -> None:
        self.login = login
        self.token = "vk1.a.живой"
        self.api_version = "5.289"


tmp = Path(tempfile.mkdtemp(prefix="empty_window_"))
settings = load_settings()
db = DB(tmp / "parser.sqlite3")

# 0. Канарейка: пустое окно принимаем за правду только когда сессия жива. Глубину
#    выдачи по «первому посту в базе» НЕ угадываем: у «бровиста» первый пост был
#    12.10, а VK на живой сессии отдаёт 05.10 (45 постов на окно в 6 ч). Поэтому
#    старые окна спрашиваются как все — раньше их закрывали без запроса (125 окон
#    участка 18.09–12.10) и эти часы навсегда выпадали из сбора.
class DepthDB:
    """База с известной выдачей: posts_in_window — посты за само окно, latest — самый свежий."""

    def __init__(self, posts_in_window: int = 0, latest: int = 1_700_010_000) -> None:
        self.posts_in_window = posts_in_window
        self.latest = latest

    def count_tag_posts_between(self, *_a) -> int:
        return self.posts_in_window

    def tag_max_date(self, *_a) -> int:
        return self.latest


class CanaryClient:
    """Канарейка отвечает постами, пока сессия жива (alive)."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.calls = 0

    def search_hashtag(self, *_a, **_kw):
        self.calls += 1
        items = [{"owner_id": -1, "post_id": 1, "date": 1}] if self.alive else []
        return {"items": items}


DEPTH = 1_700_000_000
old_job = {"tag": "бровист", "start_time": DEPTH - 7200, "end_time": DEPTH - 3600}

canary = CanaryClient(alive=True)
check(
    run_tags.empty_is_throttle(canary, DepthDB(), old_job) is False
    and canary.calls == 1,
    "окно старше первого поста в базе всё равно спрашивается: канарейка жива — "
    "пустоту принимаем, а не пропускаем окно заранее",
)
dead = CanaryClient(alive=False)
check(
    run_tags.empty_is_throttle(dead, DepthDB(), old_job) is True and dead.calls == 1,
    "канарейка молчит — сессия не отвечает, пустое окно не закрываем как «собрано»",
)
seen_canary = CanaryClient(alive=True)
check(
    run_tags.empty_is_throttle(seen_canary, DepthDB(posts_in_window=1), old_job) is True
    and seen_canary.calls == 0,
    "в базе уже есть посты за это окно — точно ограничение, канарейку не спрашиваем",
)
new_tag = CanaryClient(alive=False)
check(
    run_tags.empty_is_throttle(new_tag, DepthDB(latest=0), old_job) is False,
    "у тега без постов сравнивать не с чем — окна не пропускаем",
)

logs: list[str] = []
counts: dict[tuple, list[int]] = {}  # окно → какие count у него спрашивали
silent: list[tuple[int, int]] = []   # окна, на которые VK отвечает пустой выдачей
calls = {"windows": 0, "token_requests": 0, "token_refreshes": 0}


def fake_window(client, _db, tag, **kw):
    """VK молчит на «пустые» окна и отдаёт посты на остальные."""
    calls["windows"] += 1
    lo, hi = int(kw.get("start_time") or 0), int(kw.get("end_time") or 0)
    counts.setdefault((tag, lo, hi), []).append(int(kw.get("count") or 0))
    if any(s_lo <= lo and hi <= s_hi for s_lo, s_hi in silent):
        return {"status": "ok", "reason": "end_of_feed", "seen": 0, "new": 0, "pages": 1,
                "need_split": False, "cursor": ""}
    return {"status": "ok", "reason": "end_of_feed", "seen": 12, "new": 12, "pages": 1,
            "need_split": False, "cursor": ""}


def token_window(*_args, **_kw):
    """VK на любой запрос отвечает «токен протух», хотя фон уже выдал свежий."""
    calls["token_requests"] += 1
    return {"status": "error", "reason": "token", "seen": 0, "new": 0, "pages": 0,
            "need_split": False, "cursor": "", "error": "access_token has expired"}


def fresh_but_dead(session_path, proxy_raw="", timeout=90):
    """Обновление сессии «проходит», но VK этот токен тоже не принимает."""
    calls["token_refreshes"] += 1
    return write_session(session_path, f"vk1.a.новый-{calls['token_refreshes']}")


acc = type("Acc", (), {
    "login": "79990000001",
    "proxy": "http://u:p@1.1.1.1:80",
    "session_path": tmp / "session_79990000001.json",
    "cooldown_until": 0.0,
})()
write_session(acc.session_path, "vk1.a.живой")

# --no-browser: проверяем смену запроса и передачу окна, а не браузер (браузер проверяет
# test_session_refresh.py). Окна задаются в базе, поэтому период не ограничиваем.
args = run_tags.parse_args(["--no-browser"])
args.dry_run = False
args.no_export = True
args.tag_filter = ""
args.start_time = 0
args.end_time = 0

orig_window = run_tags.probe_mod.run_window
orig_throttle = run_tags.empty_is_throttle
orig_export = run_tags.maybe_live_export
orig_from_cookies = vk_auth.refresh_from_cookies
orig_via_browser = vk_auth.refresh_via_browser
orig_reload = vk_auth.token_via_feed_reload
orig_make_client = run_tags.make_client
orig_swap_proxy = run_tags.swap_account_proxy
run_tags.log = logs.append
run_tags.maybe_live_export = lambda *_a, **_kw: None
run_tags.empty_is_throttle = lambda *_a, **_kw: True  # канарейка: посты есть, а выдача пуста

swaps = {"calls": 0}


def no_free_proxy(*_args, **_kw) -> dict:
    """Запас прокси пуст: шаг смены адреса пропускается, лестница идёт как шла.

    Настоящий proxy_stock здесь не трогаем: тест не должен зависеть от того, что
    лежит в data/proxy_stock.txt на машине (смену прокси проверяет test_proxy_swap.py).
    """
    swaps["calls"] += 1
    return {"ok": False, "reason": "в запасе нет свободных прокси",
            "old": "", "new": "", "stock_left": 0}


run_tags.swap_account_proxy = no_free_proxy

# 0b. Старое окно (раньше первого поста в базе) прогон спрашивает у VK и собирает:
#     «первый пост в базе» — не предел выдачи VK (см. п.0).
run_tags.probe_mod.run_window = fake_window  # спросил бы VK — счётчик бы вырос
depth_run_db = DB(tmp / "depth_run.sqlite3")
depth_old = depth_run_db.add_job("маникюр", DEPTH - 7200, DEPTH - 3600, 1)
depth_queue = run_tags.JobQueue()
depth_queue.put(depth_old)
windows_before = calls["windows"]
depth_outcome, _ = run_tags.work_account(
    FakeClient(acc.login), acc, depth_queue, args, settings, depth_run_db, FakePool()
)
depth_row = depth_run_db.con.execute(
    "SELECT status, reason, seen FROM jobs WHERE id = ?", (int(depth_old["id"]),)
).fetchone()
check(
    calls["windows"] == windows_before + 1
    and depth_outcome == "empty"
    and tuple(depth_row) == ("done", "end_of_feed", 12),
    "окно старше первого поста в базе спрашивается у VK и собирается, а не пропускается",
)



# 1-3. окно, на которое VK молчит, и обычное окно в одной очереди: работа не стоит
EMPTY = (1_700_000_000, 1_700_003_600)
GOOD = (1_700_003_600, 1_700_007_200)
silent.append(EMPTY)
run_tags.probe_mod.run_window = fake_window
try:
    queue = run_tags.JobQueue()
    stuck = db.add_job("маникюр", *EMPTY, 1)
    good = db.add_job("маникюр", *GOOD, 1)
    queue.put(stuck)
    queue.put(good)
    start = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(acc.login), acc, queue, args, settings, db, FakePool()
    )
    fresh = logs[start:]
    check(
        not [line for line in fresh if "пауза" in line],
        f"на пустой выдаче пауз нет — запрос меняем сразу: "
        f"{[line for line in fresh if 'пауза' in line][:2]}",
    )
    change = next(
        (re.search(r"count (\d+) → (\d+), без паузы", line)
         for line in fresh if "другим запросом" in line),
        None,
    )
    check(
        change is not None and int(change.group(2)) < int(change.group(1)),
        "то же окно перезапрашиваем другим запросом (меньший count): "
        f"{change.group(0) if change else fresh[:3]}",
    )
    per_window = {int(k[1]): len(v) for k, v in counts.items()}
    check(
        max(per_window.values(), default=0) <= 4,
        f"одно окно не крутится бесконечно: {per_window}",
    )
    check(
        any("отдаю другому аккаунту" in line for line in fresh),
        f"пустое окно передаём другому аккаунту, а не крутим на себе: "
        f"{[line for line in fresh if 'другому аккаунту' in line][:1]}",
    )
    check(
        outcome == "empty" and reason == "очередь пуста",
        f"одно пустое окно не отправляет аккаунт в отлёжку: {outcome}/{reason}",
    )
    good_row = db.con.execute(
        "SELECT status FROM jobs WHERE id = ?", (good["id"],)
    ).fetchone()
    stuck_row = db.con.execute(
        "SELECT status FROM jobs WHERE id = ?", (stuck["id"],)
    ).fetchone()
    check(
        good_row["status"] == "done",
        f"очередь не стояла: обычное окно закрыто ({good_row['status']})",
    )
    pending = db.open_jobs(["маникюр"])
    inside = all(
        EMPTY[0] <= int(j["start_time"]) and int(j["end_time"]) <= EMPTY[1] for j in pending
    )
    covered = sum(int(j["end_time"]) - int(j["start_time"]) for j in pending)
    check(
        bool(pending) and inside and covered == EMPTY[1] - EMPTY[0],
        f"пустое окно целиком осталось в базе pending (не «done», не потеряно): "
        f"{covered} c из {EMPTY[1] - EMPTY[0]}, status={stuck_row['status']}",
    )
    trail = [
        e for e in db.recent_events(50)
        if e["level"] == "warn" and "молча" in str(e["message"])
    ]
    check(
        len(trail) == 1 and trail[0]["tag"] == "маникюр" and trail[0]["account"],
        f"пустое окно видно в панели/логе событий, и ровно один раз на окно: "
        f"{[(e['kind'], e['account'], e['message']) for e in trail]}",
    )

    # 4. «access_token has expired» и после обновления сессии: окно не крутится по кругу
    run_tags.probe_mod.run_window = token_window
    vk_auth.refresh_via_browser = fresh_but_dead
    vk_auth.refresh_from_cookies = fresh_but_dead
    queue2 = run_tags.JobQueue()
    job2 = db.add_job("теннис", *EMPTY, 1)
    queue2.put(job2)
    start = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(acc.login), acc, queue2, args, settings, db, FakePool()
    )
    fresh = logs[start:]
    check(
        outcome == "dead" and "повторный вход" in reason,
        f"сессия кончилась — аккаунт выбывает с понятной причиной: {outcome}/{reason}",
    )
    check(
        calls["token_refreshes"] == run_tags.EMPTY_TOKEN_REFRESH_RETRIES,
        f"сессию обновляем ограниченное число раз: {calls['token_refreshes']}",
    )
    check(
        calls["token_requests"] == run_tags.EMPTY_TOKEN_REFRESH_RETRIES + 1,
        f"запросы к VK тоже ограничены (не бесконечный F5): {calls['token_requests']}",
    )
    check(
        any("не принят и после" in line for line in fresh),
        f"в логе видно, что дело в сессии, а не в окне: "
        f"{[line for line in fresh if 'не принят' in line][:1]}",
    )
    check(
        queue2.size() == 1 and not queue2.drained(),
        f"окно осталось в очереди — его возьмёт живой аккаунт: {queue2.size()}",
    )
    row2 = db.con.execute(
        "SELECT status, reason FROM jobs WHERE id = ?", (job2["id"],)
    ).fetchone()
    check(
        row2["status"] == "pending" and row2["reason"] == "token",
        f"окно в базе pending (его добьёт следующий «Старт»), а не «running» навсегда: "
        f"{row2['status']}/{row2['reason']}",
    )
    dead_trail = [
        e for e in db.recent_events(50)
        if e["level"] == "error" and "повторный вход" in str(e["message"])
    ]
    check(
        len(dead_trail) == 1,
        f"в панели видно, почему аккаунт выбыл из прогона: "
        f"{[e['message'] for e in dead_trail]}",
    )
    # 5. Пустые выдачи идут серией: лестница проходит один раз на окно и один раз на
    #    аккаунт за прогон, а не заново после каждой отлёжки. Раньше счётчики жили
    #    внутри work_account: аккаунт возвращался из отлёжки и снова жал F5, менял count
    #    и дробил окна — на 13 молча пустых окон уходило под сотню запросов и десяток
    #    открытий браузера (в логе прогона «пустых выдач 93»).
    # Общий счётчик «тихого нуля» (по всем аккаунтам) считаем с нуля: этот тест — про
    # счётчики ОДНОГО аккаунта, общий тормоз здесь не должен вмешиваться.
    run_tags.silent_reset()
    SERIES = (1_700_030_000, 1_700_030_000 + 5 * 3600)
    silent.append(SERIES)
    f5 = {"calls": 0}

    def fake_reload(session_path, proxy_raw="", timeout=90, headless=True):
        """F5 в профиле: как настоящий vk_auth пишет в файл сессии свежий токен.

        headless в заглушке — просто как в настоящем vk_auth: в настройках может
        стоять show_browser (тогда прогон зовёт F5 с headless=False), и заглушка не
        должна от этого ломаться.
        """
        f5["calls"] += 1
        return write_session(session_path, "vk1.a.после-F5")

    run_tags.probe_mod.run_window = fake_window  # окна серии снова молчат
    run_tags.empty_is_throttle = lambda *_a, **_kw: True
    vk_auth.refresh_from_cookies = orig_from_cookies
    vk_auth.refresh_via_browser = orig_via_browser
    vk_auth.token_via_feed_reload = fake_reload
    run_tags._browser_prefer_until.clear()
    series_args = run_tags.parse_args([])  # без --no-browser: проверяем и бюджет F5
    series_args.dry_run = False
    series_args.no_export = True
    series_args.tag_filter = ""
    series_args.start_time = 0
    series_args.end_time = 0
    series = [
        db.add_job("ногти", SERIES[0] + i * 3600, SERIES[0] + (i + 1) * 3600, 1)
        for i in range(5)
    ]
    per_window.clear()
    queue3 = run_tags.JobQueue()
    for job in series:
        queue3.put(job)
    start = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(acc.login), acc, queue3, series_args, settings, db, FakePool()
    )
    series_log = logs[start:]
    check(
        outcome == "hold" and reason == "ВК отдаёт пустую выдачу",
        f"серия пустых окон отправляет аккаунт в отлёжку: {outcome}/{reason}",
    )
    check(
        f5["calls"] == run_tags.EMPTY_F5_RETRIES,
        f"F5 — на аккаунт за прогон, а не на каждое окно: вызовов {f5['calls']} "
        f"(ожидали {run_tags.EMPTY_F5_RETRIES})",
    )
    check(
        swaps["calls"] >= run_tags.EMPTY_PROXY_SWAPS_PER_RUN
        and len([line for line in logs if "сменить нечем" in line]) == 1,
        f"адрес аккаунта пробуем сменить, а если менять нечем — одна строка на аккаунт: "
        f"попыток {swaps['calls']}, "
        f"строки {[line for line in logs if 'прокси' in line][:2]}",
    )
    check(
        len([line for line in series_log if "делю окно пополам" in line]) == 1,
        f"окна дробим по разу за прогон: "
        f"{[line for line in series_log if 'пополам' in line]}",
    )
    series_calls = calls["windows"]

    # Тот же аккаунт вернулся из отлёжки и добирает остаток окон: лестницу он больше не
    # повторяет — каждое окно берётся одним запросом, окно остаётся базе pending.
    before = {key: len(vals) for key, vals in counts.items()}
    queue4 = run_tags.JobQueue()
    for job in series[3:]:
        queue4.put(job)
    f5_before = f5["calls"]
    start = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(acc.login), acc, queue4, series_args, settings, db, FakePool()
    )
    quiet_log = logs[start:]
    check(
        outcome == "empty" and reason == "очередь пуста",
        f"после отлёжки аккаунт доедает окна, а не уходит в отлёжку по кругу: "
        f"{outcome}/{reason}",
    )
    check(
        f5["calls"] == f5_before,
        f"из отлёжки аккаунт возвращается без нового F5: добавилось "
        f"{f5['calls'] - f5_before}",
    )
    check(
        not [
            line for line in quiet_log
            if "другим запросом" in line or "пополам" in line
        ],
        f"лестницу не повторяем — запросы к VK не множатся: "
        f"{[line for line in quiet_log if 'запрос' in line or 'пополам' in line][:2]}",
    )
    per_request = {
        key: len(vals) - before.get(key, 0)
        for key, vals in counts.items()
        if len(vals) != before.get(key, 0)
    }
    check(
        bool(per_request) and all(count == 1 for count in per_request.values()),
        f"в тихом режиме окно берётся одним запросом: {per_request}",
    )
    check(
        calls["windows"] - series_calls <= 8,
        f"серия из 5 пустых окон стоит запросов к VK: {calls['windows'] - series_calls}",
    )
    quiet_trail = [
        e for e in db.recent_events(50)
        if e["level"] == "warn" and "не повторяю" in str(e["message"])
    ]
    check(
        len(quiet_trail) == 1 and quiet_trail[0]["account"] == acc.login,
        f"в панели видно, что лестницу больше не крутят: "
        f"{[e['message'] for e in quiet_trail]}",
    )

    # 6. «Тихий ноль» сразу у всех аккаунтов: если VK режет выдачу каждому, лестница и
    #    передача окон бесполезны — только лишние запросы и сотни одинаковых строк в
    #    логе, а очередь выгорает без постов. Такой режим прерывает раунд одной строкой,
    #    недобитые окна остаются pending — их добьёт следующий заход.
    run_tags.EMPTY_GLOBAL_QUIET = 3
    run_tags.EMPTY_GLOBAL_STOP = 4
    run_tags.silent_reset()
    quiet_login = "79990000007"
    quiet_acc = type("Acc", (), {
        "login": quiet_login,
        "proxy": "http://u:p@1.1.1.1:80",
        "session_path": tmp / f"session_{quiet_login}.json",
        "cooldown_until": 0.0,
    })()
    write_session(quiet_acc.session_path, "vk1.a.живой")
    quiet_args = run_tags.parse_args(["--no-browser"])
    quiet_args.dry_run = False
    quiet_args.no_export = True
    quiet_args.tag_filter = ""
    quiet_args.start_time = 0
    quiet_args.end_time = 0
    QUIET_TAG = "лак"
    QUIET_START = 1_700_100_000
    silent.append((QUIET_START, QUIET_START + 8 * 3600))
    quiet_queue = run_tags.JobQueue()
    for i in range(8):
        quiet_queue.put(db.add_job(QUIET_TAG, QUIET_START + i * 3600,
                                   QUIET_START + (i + 1) * 3600, 1))
    start = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(quiet_login), quiet_acc, quiet_queue, quiet_args, settings, db, FakePool()
    )
    quiet_log = logs[start:]
    check(
        outcome == "silent_stop" and "всем аккаунтам" in reason,
        f"пусто у всех — раунд прерываем, а не молотим очередь: {outcome}/{reason}",
    )
    check(
        run_tags.silent_stopped() and quiet_queue.size() >= 1,
        f"недобитые окна остались в очереди (не потеряны): {quiet_queue.size()}",
    )
    global_note = [line for line in quiet_log if "по всем аккаунтам" in line]
    check(
        len(global_note) == 1,
        f"про общий «тихий ноль» в логе ровно одна строка: {global_note}",
    )
    per_window_lines = [
        line for line in quiet_log
        if "оставляю следующему" in line or "отдаю другому аккаунту" in line
    ]
    check(
        len(per_window_lines) <= 3,
        f"в общем «тихом» режиме по каждому окну в лог не пишем (не флудим): "
        f"{per_window_lines}",
    )
    check(
        len(db.open_jobs([QUIET_TAG])) >= 1,
        f"пустые окна в базе pending — их добьёт следующий «Старт»: "
        f"{len(db.open_jobs([QUIET_TAG]))}",
    )
    run_tags.silent_reset()
    run_tags.EMPTY_GLOBAL_QUIET = 10
    run_tags.EMPTY_GLOBAL_STOP = 40

    # 7. То же на уровне раунда (run_round → run_worker → work_account): раунд
    #    останавливается и очередь остаётся, а аккаунт не уходит в отлёжку — тормоз
    #    общий (VK режет всех), а не про конкретный аккаунт.
    from account_pool import AccountPool, PoolAccount
    run_tags.EMPTY_GLOBAL_QUIET = 3
    run_tags.EMPTY_GLOBAL_STOP = 4
    run_tags.silent_reset()
    round_login = "79990000008"
    round_session = tmp / f"session_{round_login}.json"
    write_session(round_session, "vk1.a.живой")
    round_pool = AccountPool([PoolAccount(
        login=round_login, proxy="http://u:p@1.1.1.1:80", session_path=round_session,
    )])
    run_tags.make_client = lambda *_a, **_kw: FakeClient(round_login)
    ROUND_TAG = "гель"
    ROUND_START = 1_700_200_000
    silent.append((ROUND_START, ROUND_START + 10 * 3600))
    round_jobs = [
        db.add_job(ROUND_TAG, ROUND_START + i * 3600, ROUND_START + (i + 1) * 3600, 1)
        for i in range(10)
    ]
    start = len(logs)
    left = run_tags.run_round(round_jobs, round_pool, 1, quiet_args, settings, db)
    run_tags.make_client = orig_make_client
    round_log = logs[start:]
    check(
        run_tags.silent_stopped() and left > 0,
        f"раунд прерывается, а очередь остаётся (не выжигаем): left={left}",
    )
    check(
        [(s["login"], s["status"]) for s in round_pool.snapshot()] == [(round_login, "ready")],
        f"аккаунт не в отлёжке и не мёртв: {round_pool.snapshot()}",
    )
    check(
        len([
            line for line in round_log
            if "оставляю следующему" in line or "отдаю другому аккаунту" in line
        ]) <= 3,
        "и на уровне раунда по каждому окну в лог не пишем",
    )
    run_tags.silent_reset()
    run_tags.EMPTY_GLOBAL_QUIET = 10
    run_tags.EMPTY_GLOBAL_STOP = 40
finally:
    run_tags.probe_mod.run_window = orig_window
    run_tags.empty_is_throttle = orig_throttle
    run_tags.maybe_live_export = orig_export
    vk_auth.refresh_from_cookies = orig_from_cookies
    vk_auth.refresh_via_browser = orig_via_browser
    vk_auth.token_via_feed_reload = orig_reload
    run_tags.make_client = orig_make_client
    run_tags.swap_account_proxy = orig_swap_proxy
    run_tags._browser_prefer_until.clear()

db.close()
shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ПУСТОЕ ОКНО НЕ ТОРМОЗИТ ПРОГОН")
raise SystemExit(1 if FAILED else 0)
