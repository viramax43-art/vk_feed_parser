#!/usr/bin/env python3
"""Прогон по списку хештегов: все аккаунты работают параллельно над окнами времени.

Запуск:
    python src/run_tags.py data/pilot.txt      # свой список тегов
    python src/run_tags.py --hours 6           # только последние 6 часов
    python src/run_tags.py --passes 5          # + 4 повторных прохода за новыми постами
    python src/run_tags.py --watch             # повторять проходы, пока не остановят
    python src/run_tags.py --no-resume         # не добивать прерванный прогон
    python src/run_tags.py --validate-first    # проверить аккаунты, потом запустить

Тег режется на окна по window_hours, окна идут в общую очередь, каждый аккаунт
берёт следующее свободное. Если VK обрезал выдачу окна (нашёл больше, чем отдал),
окно делится пополам. Повторный проход берёт только [дата последнего найденного
поста − нахлёст, сейчас]. Прогресс окон и курсоры лежат в SQLite: после Ctrl+C
запуск продолжит с того же места.

Аккаунтов может быть больше, чем воркеров (--workers): они работают пулом с
ротацией. Аккаунт ушёл в отлёжку (капча, лимиты, пустые выдачи) или выбыл (нужен
повторный вход) — воркер берёт следующий живой; статусы смотреть через
python src/account_pool.py --status.

Аккаунты — data/accounts.txt, сессии — data/session.json и
data/session_<телефон>.json (python src/vk_auth.py --login).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe as probe_mod  # noqa: E402
from account_pool import (  # noqa: E402
    DEAD,
    HOLD,
    READY,
    AccountPool,
    PoolAccount,
    load_pool_accounts,
    validate_accounts,
)
from accounts import Account, load_accounts  # noqa: E402
from export_xlsx import export_posts  # noqa: E402
from paths import ACCOUNTS_FILE, EXPORTS_DIR, SESSION_FILE, TAGS_FILE  # noqa: E402
from settings import load_settings  # noqa: E402
from state_db import DB  # noqa: E402
from vk_client import VkFeedClient  # noqa: E402

_print_lock = threading.Lock()
_stop_flag = threading.Event()
_export_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {"jobs": 0, "split": 0, "new": 0, "pages": 0, "errors": 0, "captcha": 0, "token": 0,
          "throttle": 0}
# Паузы аккаунта, если VK молча отдаёт пустую выдачу (проверяется канарейкой).
THROTTLE_PAUSES = (30, 90, 180, 300, 600)


def empty_is_throttle(client, db: DB, job: dict) -> bool:
    """Пустое окно — правда пусто или ВК молча режет выдачу?

    1) в базе уже есть посты за это окно → точно ограничение;
    2) иначе «канарейка»: час до самого свежего известного поста тега —
       там посты есть гарантированно. Пусто и там → ограничение.
    """
    tag, start, end = job["tag"], int(job["start_time"]), int(job["end_time"])
    if db.count_tag_posts_between(tag, start, end):
        return True
    latest = db.tag_max_date(tag)
    if not latest:
        return False  # тег новый, сравнить не с чем
    try:
        probe = client.search_hashtag(tag, count=10, start_time=latest - 3600, end_time=latest + 1)
    except Exception:  # noqa: BLE001 — ошибку канарейки считаем ограничением
        return True
    return not probe.get("items")


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_ts(ts: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(int(ts)))


def bump(**kwargs) -> None:
    with _stats_lock:
        for key, value in kwargs.items():
            _stats[key] = _stats.get(key, 0) + int(value)


def snapshot() -> dict:
    with _stats_lock:
        return dict(_stats)


def parse_tags_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("//") or (s.startswith("#") and s[1:2].isspace()):
            continue  # пустая строка или комментарий: «// заметка» и «# заметка»
        tag = s.lstrip("#").strip()
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


class JobQueue:
    """Очередь окон. Поток выходит, только когда очередь пуста И никто не
    держит окно в работе (иначе пропустим половинки от дробления)."""

    def __init__(self) -> None:
        self._items: deque = deque()
        self._cv = threading.Condition()
        self._active = 0

    def put(self, job: dict, front: bool = False) -> None:
        with self._cv:
            (self._items.appendleft if front else self._items.append)(job)
            self._cv.notify_all()

    def get(self) -> Optional[dict]:
        with self._cv:
            while True:
                if _stop_flag.is_set():
                    return None
                if self._items:
                    self._active += 1
                    return self._items.popleft()
                if self._active == 0:
                    return None
                self._cv.wait(timeout=0.5)

    def done(self) -> None:
        with self._cv:
            self._active -= 1
            self._cv.notify_all()

    def size(self) -> int:
        with self._cv:
            return len(self._items)

    def drained(self) -> bool:
        """Пусто и никто не держит окно — воркерам больше нечего делать."""
        with self._cv:
            return not self._items and self._active == 0


def build_pool(
    accounts: list[Account], db: DB, workers: int, proxy_override: str = ""
) -> tuple[AccountPool, int]:
    """Пул аккаунтов и число воркеров. Без accounts.txt — одна общая сессия.

    Пул вмещает ВСЕ аккаунты, а воркеров — сколько задано: упавший/ушедший в
    отлёжку аккаунт заменяется следующим живым, простой не копится.
    """
    if not accounts:
        items = [PoolAccount("session", proxy_override, SESSION_FILE)]
    else:
        items = load_pool_accounts(accounts, db, proxy_override)
    pool = AccountPool(items)
    alive = pool.alive()
    if not alive:
        return pool, 0
    threads = workers if workers and workers > 0 else alive
    return pool, max(1, min(threads, alive))


def describe_pool(pool: AccountPool) -> str:
    snap = pool.snapshot()
    ready = sum(1 for s in snap if s["status"] == READY)
    hold = sum(1 for s in snap if s["status"] == HOLD)
    dead = sum(1 for s in snap if s["status"] == DEAD)
    return f"аккаунтов {len(snap)} (готовы {ready}, отлёжка {hold}, мертвы {dead})"


def pool_has_ready(pool: AccountPool) -> bool:
    return any(s["status"] == READY for s in pool.snapshot())


def make_client(session_path: Path, proxy: str, pause: float, label: str):
    if not Path(session_path).exists():
        log(f"{label}: нет файла сессии {session_path} — вход: python src/vk_auth.py --login")
        return None
    try:
        return VkFeedClient.from_session(Path(session_path), proxy, pause=pause)
    except Exception as exc:  # noqa: BLE001
        log(f"{label}: не удалось создать клиент ({exc})")
        return None


_browser_lock = threading.Lock()  # скрытые браузеры обновляем по одному — меньше нагрузки и подозрений


def _refresh_client(client, session_path: Path, proxy: str, label: str) -> bool:
    """Новый токен: сначала через профиль браузера (работает и когда ВК режет
    обмен cookies ошибкой «too much tries»), затем по cookies. True — клиент жив."""
    from vk_auth import refresh_from_cookies, refresh_via_browser

    attempts = (
        ("профиль браузера", lambda: refresh_via_browser(Path(session_path), proxy)),
        ("cookies", lambda: refresh_from_cookies(Path(session_path), proxy)),
    )
    for name, fn in attempts:
        try:
            if name == "профиль браузера":
                with _browser_lock:
                    payload = fn()
            else:
                payload = fn()
        except Exception as exc:  # noqa: BLE001
            log(f"{label}: токен через {name} не обновлён ({exc})")
            continue
        token = str(payload.get("access_token") or "")
        if token:
            client.token = token
            client.api_version = payload.get("api_version") or client.api_version
            log(f"{label}: токен обновлён через {name}")
            return True
    return False


def split_windows(start: int, end: int, size_sec: int) -> list[tuple[int, int]]:
    """[start, end) → окна по size_sec, самые свежие первыми."""
    out: list[tuple[int, int]] = []
    hi = end
    while hi > start:
        lo = max(start, hi - size_sec)
        out.append((lo, hi))
        hi = lo
    return out


def split_job(db: DB, job: dict, queue: JobQueue, min_sec: int) -> bool:
    """Делит окно пополам, если выдача обрезана лимитом (~60 постов на запрос).

    Делим по времени, а не «ниже самого старого поста»: VK не гарантирует
    порядок выдачи, а половины добирают всё. False — мельче min_sec не делим.
    """
    start, end = int(job["start_time"]), int(job["end_time"])
    if end - start < 2 * min_sec:
        return False
    mid = start + (end - start) // 2
    db.update_job(job["id"], status="split", cursor="")
    for lo, hi in ((start, mid), (mid, end)):  # (mid, end) окажется первым
        queue.put(db.add_job(job["tag"], lo, hi, job["round"]), front=True)
    return True


def handle_result(db: DB, job: dict, res: dict, queue: JobQueue, settings: dict, label: str) -> str:
    """Решает судьбу окна. Возвращает 'ok' | 'stop' (слот больше не работает)."""
    tag = job["tag"]
    window = f"#{tag} {fmt_ts(job['start_time'])}–{fmt_ts(job['end_time'])}"
    bump(pages=res["pages"], new=res["new"])
    stats = {"seen": int(job.get("seen") or 0) + res["seen"], "new": int(job.get("new") or 0) + res["new"],
             "pages": int(job.get("pages") or 0) + res["pages"]}

    if res["status"] == "ok":
        min_sec = int(settings["min_window_minutes"]) * 60
        if res["need_split"] and split_job(db, job, queue, min_sec):
            bump(split=1)
            log(f"{label}: {window}: получено {res['seen']} — делю окно пополам")
            return "ok"
        db.update_job(job["id"], status="done", cursor="", reason=res["reason"], **stats)
        bump(jobs=1)
        log(f"{label}: {window}: постов {stats['seen']}, новых {stats['new']} ({res['reason']})")
        return "ok"

    reason = res["reason"]
    bump(errors=1)
    db.event("error", "slot", f"{label}: {window} — {reason}", account=label, tag=tag)
    db.update_job(job["id"], status="pending", reason=reason, **stats)
    job.update(stats, cursor=res.get("cursor") or job.get("cursor"))
    if reason in ("token", "captcha"):
        queue.put(job, front=True)  # окно отдаём живому аккаунту
        return "stop"
    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    if attempts >= int(settings["job_attempts"]):
        db.update_job(job["id"], status="failed", attempts=attempts)
        log(f"{label}: {window}: окно брошено после {attempts} попыток ({reason})")
    else:
        db.update_job(job["id"], attempts=attempts)
        log(f"{label}: {window}: ошибка {reason}, повтор {attempts}/{settings['job_attempts']}")
        queue.put(job)
    return "ok"


def save_account_state(db: DB, acc: PoolAccount, outcome: str, reason: str) -> None:
    """Пишет в базу статус аккаунта после работы воркера.

    ok и empty — успех (аккаунт цел, очередь просто опустела).
    """
    if outcome in ("ok", "empty"):
        return
    if outcome == "hold":
        db.mark_account(acc.login, proxy=acc.proxy, status=HOLD, error=reason,
                        cooldown_until=acc.cooldown_until, bump_fail=True)
    else:
        db.mark_account(acc.login, proxy=acc.proxy, status=DEAD, error=reason, bump_fail=True)


def run_worker(pool: AccountPool, queue: JobQueue, args, settings: dict, db: DB) -> None:
    """Один поток: берёт аккаунт из пула и добивает очередь им.

    Аккаунт выбыл (отлёжка/мёртв) — берём из пула следующий.
    Очередь опустела — работа закончена.
    """
    while not queue.drained():
        acc = pool.acquire(timeout=15)
        if acc is None:
            if pool.alive() == 0 or queue.drained():
                break
            continue
        client = make_client(
            acc.session_path, acc.proxy, float(settings["request_pause_sec"]), acc.login
        )
        if client is None:
            pool.release(acc, "dead", "нет сессии/токена")
            if not args.dry_run:
                db.mark_account(acc.login, proxy=acc.proxy, status=DEAD,
                                error="нет сессии/токена", bump_fail=True)
            continue
        outcome, reason = work_account(client, acc, queue, args, settings, db, pool)
        # «empty» — это успех (очередь опустела), а не сбой аккаунта: без cooldown
        pool.release(acc, "ok" if outcome == "empty" else outcome, reason)
        if not args.dry_run:
            save_account_state(db, acc, outcome, reason)
        if outcome == "hold":
            log(f"{acc.login}: отлёжка («{reason}») — беру следующий аккаунт")
        elif outcome == "dead":
            log(f"{acc.login}: мёртв («{reason}») — беру следующий аккаунт")
        if outcome == "empty":
            break


def work_account(
    client, acc: PoolAccount, queue: JobQueue, args, settings: dict, db: DB, pool: AccountPool
) -> tuple[str, str]:
    """Крутит окна одним аккаунтом. Исход: empty (очередь пуста) / hold / dead."""
    label, proxy, session_path = acc.login, acc.proxy, acc.session_path
    empty_strikes = 0
    while True:
        job = queue.get()
        if job is None:
            return ("empty", "очередь пуста")
        try:
            db.update_job(job["id"], status="running")
            res = probe_mod.run_window(
                client, None if args.dry_run else db, job["tag"],
                start_time=int(job["start_time"]), end_time=int(job["end_time"]),
                count=args.count, max_pages=args.pages, cursor=job.get("cursor") or "",
                stop_when_no_new=int(settings["stop_when_no_new_pages"]),
                dry_run=args.dry_run, round_no=int(job["round"]),
                seen_before=int(job.get("seen") or 0),
                split_if_seen=int(settings["split_if_seen"]),
                on_page=lambda cur, _n, _f, jid=job["id"]: db.update_job(jid, cursor=cur),
            )
            if res["status"] == "error" and res["reason"] == "token":
                bump(token=1)
                if _refresh_client(client, session_path, proxy, label):
                    queue.put(job, front=True)  # тот же аккаунт, свежий токен
                    continue
                queue.put(job, front=True)  # окно достанется живому аккаунту
                return ("dead", "нужен повторный вход")
            if res["status"] == "error" and res["reason"] == "captcha":
                bump(captcha=1)
                queue.put(job, front=True)
                return ("hold", "капча от VK")
            if res["status"] == "ok" and res["seen"] == 0 and not args.dry_run and \
                    empty_is_throttle(client, db, job):
                # ВК вернул пусто, а проверка показала, что посты есть, — тихое ограничение
                empty_strikes += 1
                bump(throttle=1)
                db.update_job(job["id"], status="pending", reason="empty_throttle")
                queue.put(job, front=True)
                if empty_strikes > len(THROTTLE_PAUSES):
                    log(f"{label}: ВК отдаёт пустую выдачу {empty_strikes} раз подряд — "
                        "аккаунт в отлёжку, очередь добирают другие")
                    return ("hold", "ВК отдаёт пустую выдачу")
                pause = THROTTLE_PAUSES[empty_strikes - 1]
                log(f"{label}: ВК молча отдаёт пустую выдачу (проверено), "
                    f"пауза {pause} с ({empty_strikes}/{len(THROTTLE_PAUSES)})")
                if _stop_flag.wait(pause):  # Ctrl+C прерывает паузу сразу
                    return ("hold", "остановка")
                continue
            if res["status"] == "ok" and res["seen"] == 0 and not args.dry_run and \
                    not job.get("empty_checked") and db.tag_max_date(job["tag"]):
                # канарейка в порядке, но ограничение могло отпустить между запросами —
                # пустое окно принимаем только со второго раза
                job["empty_checked"] = True
                db.update_job(job["id"], status="pending", reason="empty_recheck")
                queue.put(job, front=True)
                continue
            if res["seen"]:
                empty_strikes = 0
            if handle_result(db, job, res, queue, settings, label) == "stop":
                return ("hold", str(res.get("reason") or "аккаунт выбыл"))
            if not args.dry_run:
                db.add_account_items(label, res["new"])
                db.mark_account(label, proxy=proxy, status=READY, error="", touch_ok=True)
            pool.add_posts(acc, res["new"])
            maybe_live_export(db, settings, args)
        except Exception as exc:  # noqa: BLE001 — поток не должен умирать молча
            log(f"{label}: сбой на окне #{job['tag']}: {type(exc).__name__}: {exc}")
            db.update_job(job["id"], status="pending", reason=f"crash: {exc}")
            queue.put(job)
            bump(errors=1)
            time.sleep(2)
        finally:
            queue.done()


_last_export = {"t": 0.0}


def maybe_live_export(db: DB, settings: dict, args) -> None:
    """Живой Excel: пишет только один поток за раз, остальные не ждут."""
    if args.no_export or args.dry_run:
        return
    if time.time() - _last_export["t"] < max(5, int(settings["export_interval_sec"])):
        return
    if not _export_lock.acquire(blocking=False):
        return
    try:
        _last_export["t"] = time.time()
        export_posts(db, EXPORTS_DIR / "vk_feed_live.xlsx", tag=args.tag_filter or None)
    except Exception as exc:  # noqa: BLE001
        log(f"живой экспорт не удался: {exc}")
    finally:
        _export_lock.release()


def subtract_intervals(start: int, end: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) минус уже собранные отрезки → дыры; дыры короче минуты отбрасываем."""
    gaps, cur = [], start
    for lo, hi in covered:
        if hi <= cur or lo >= end:
            continue
        if lo > cur:
            gaps.append((cur, lo))
        cur = max(cur, hi)
    if cur < end:
        gaps.append((cur, end))
    return [(a, b) for a, b in gaps if b - a >= 60]


def plan_round(db: DB, tags: list[str], args, settings: dict, round_no: int) -> list[dict]:
    """Окна на раунд: 1-й — весь период нарезкой, следующие — только свежее."""
    # +1 c к «сейчас»: пост, опубликованный в секунду запуска, не выпадет
    end = int(args.end_time) if args.end_time else int(time.time()) + 1
    if round_no == 1:
        size = int(float(settings["window_hours"]) * 3600)
        jobs = []
        for tag in tags:
            covered = [] if args.full else db.covered_intervals(tag, int(settings["split_if_seen"]))
            gaps = subtract_intervals(int(args.start_time), end, covered)
            skipped = (end - int(args.start_time)) - sum(b - a for a, b in gaps)
            if skipped > 60:
                log(f"#{tag}: уже собрано {skipped / 3600:.1f} ч из периода — пропускаю (--full — пройти всё)")
            for g_lo, g_hi in gaps:
                jobs += [db.add_job(tag, lo, hi, 1) for lo, hi in split_windows(g_lo, g_hi, size)]
        return jobs
    overlap = int(settings["repeat_overlap_sec"])
    jobs = []
    for tag in tags:
        since = max(int(args.start_time), db.tag_max_date(tag) - overlap)
        if since < end:
            jobs.append(db.add_job(tag, since, end, round_no))
    return jobs


def run_round(jobs: list[dict], pool: AccountPool, workers: int, args, settings: dict, db: DB) -> int:
    """Гоняет окна воркерами (аккаунт берётся из пула); возвращает остаток очереди."""
    queue = JobQueue()
    for job in jobs:
        queue.put(job)
    threads = [
        threading.Thread(target=run_worker, args=(pool, queue, args, settings, db), name=f"acc{i + 1}")
        for i in range(max(1, workers))
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1.0)
    except KeyboardInterrupt:
        log("остановка по Ctrl+C — дожидаюсь текущих запросов (прогресс сохранён)")
        _stop_flag.set()
        for t in threads:
            t.join(timeout=60)
    return queue.size()


def sleep_interruptible(seconds: int) -> bool:
    """False — прервали Ctrl+C."""
    try:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(1.0, end - time.time()))
        return True
    except KeyboardInterrupt:
        _stop_flag.set()
        return False


def finish_tags(db: DB, tags: list[str], args) -> None:
    if args.dry_run:
        return
    for tag in tags:
        left = db.count_open_jobs(tag)
        db.finish_tag(tag, status="partial" if left else "finished",
                      reason=f"окон не добито: {left}" if left else "all_windows_done")


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Прогон по хештегам VK (окна времени, многопоточно)")
    p.add_argument("listing", nargs="?", default="", help="файл со списком тегов")
    p.add_argument("--pages", type=int, default=0, help="макс. страниц на одно окно")
    p.add_argument("--count", type=int, default=0, help="постов на страницу (10..200)")
    p.add_argument("--passes", type=int, default=0, help="раундов всего (1 = без повторов)")
    p.add_argument("--watch", action="store_true", help="повторять раунды без конца")
    p.add_argument("--repeat-pause", type=int, default=0)
    p.add_argument("--hours", type=float, default=0)
    p.add_argument("--days", type=float, default=0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--window-hours", type=float, default=0, help="размер стартового окна")
    p.add_argument("--workers", type=int, default=0, help="сколько аккаунтов задействовать")
    p.add_argument("--validate", action="store_true", help="проверить ВСЕ аккаунты и выйти")
    p.add_argument("--validate-first", action="store_true", help="проверить аккаунты, затем запустить")
    p.add_argument("--validate-workers", type=int, default=4, help="сколько аккаунтов проверять разом")
    p.add_argument("--no-validate", action="store_true", help="не проверять аккаунты, даже если готовых нет")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер при обновлении токена")
    p.add_argument("--proxy", default="", help="прокси для всех слотов")
    p.add_argument("--limit", type=int, default=0, help="взять первые N тегов")
    p.add_argument("--no-resume", action="store_true", help="бросить прерванные окна, начать заново")
    p.add_argument("--full", action="store_true", help="пройти весь период, даже уже собранные часы")
    p.add_argument("--dry-run", action="store_true", help="не писать в базу")
    p.add_argument("--no-export", action="store_true", help="без живого Excel-экспорта")
    p.add_argument("--tag-filter", default="", help="для экспорта: только один тег")
    # совместимость со старыми командами — теперь не нужны
    p.add_argument("--retry", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--reset", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def resolve_period(args, settings: dict) -> None:
    now = int(time.time())
    args.start_time = probe_mod.parse_time(args.since)
    args.end_time = probe_mod.parse_time(args.until)
    if args.hours or args.days:
        args.start_time = int(now - (args.hours * 3600 + args.days * 86400))
    if not args.start_time:
        # без start_time VK отдаёт только последние 24 часа — задаём явно
        args.start_time = int(now - float(settings["default_days"]) * 86400)
    if args.end_time and args.start_time >= args.end_time:
        raise SystemExit("--since должен быть меньше --until")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    if args.window_hours:
        settings["window_hours"] = args.window_hours
    args.pages = args.pages or int(settings["max_pages_per_tag"])
    args.count = min(200, max(10, args.count or int(settings["page_count"])))
    passes = 10**9 if args.watch else (args.passes or int(settings["repeat_passes"]))
    repeat_pause = args.repeat_pause or int(settings["repeat_pause_sec"])
    resolve_period(args, settings)

    listing = Path(args.listing) if args.listing else TAGS_FILE
    tags = parse_tags_file(listing)[: args.limit or None]
    if not tags:
        log(f"нет тегов в {listing} — добавьте по одному на строку")
        return 1

    db = DB()
    accounts = load_accounts(ACCOUNTS_FILE)

    # Проверка аккаунтов (независимо от числа рабочих потоков): --validate — только
    # проверить и выйти, --validate-first — проверить и сразу работать.
    if args.validate or args.validate_first:
        log(f"проверяю аккаунты ({len(accounts) or 1} шт., разом до {args.validate_workers})…")
        summary = validate_accounts(
            db,
            accounts,
            workers=args.validate_workers,
            tag=tags[0] if tags else "",
            proxy_override=args.proxy,
            use_browser=not args.no_browser,
            on_result=lambda r: log(f"  {r['login']}: {r['status']} — {r['reason']}"),
        )
        log(
            f"проверка: всего {summary['total']}, готовы {summary['ready']}, "
            f"отлёжка {summary['hold']}, мертвы {summary['dead']}"
        )
        if args.validate and not args.validate_first:
            return 0 if summary["dead"] == 0 else 2

    pool, workers = build_pool(
        accounts, db, args.workers or int(settings["max_accounts"]), args.proxy
    )
    # Готовых нет (например, у всех протух токен) — один раз проверяем все: проверка
    # умеет обновлять токен из cookies или профиля браузера, так аккаунты оживают.
    # Если проверка уже была выше (--validate / --validate-first), не гоняем её снова.
    checked = bool(args.validate or args.validate_first)
    if not checked and not args.dry_run and not args.no_validate and not pool_has_ready(pool):
        log("нет готовых аккаунтов — проверяю все (токен обновляется автоматически)…")
        summary = validate_accounts(
            db,
            accounts,
            workers=args.validate_workers,
            tag=tags[0] if tags else "",
            proxy_override=args.proxy,
            use_browser=not args.no_browser,
            on_result=lambda r: log(f"  {r['login']}: {r['status']} — {r['reason']}"),
        )
        log(
            f"проверка: готовы {summary['ready']}, отлёжка {summary['hold']}, "
            f"мертвы {summary['dead']}"
        )
        pool, workers = build_pool(
            accounts, db, args.workers or int(settings["max_accounts"]), args.proxy
        )
    if workers == 0:
        log("нет живых аккаунтов — проверьте вход (python src/account_pool.py --validate)")
        return 2

    # Недобитые окна прошлого запуска + всегда окна текущего периода:
    # иначе при наличии старых окон свежие часы не собирались бы.
    leftover = [] if args.no_resume else db.open_jobs(tags)
    if args.no_resume:
        log(f"брошено прерванных окон: {db.drop_open_jobs(tags)}")
    jobs = plan_round(db, tags, args, settings, 1)
    planned_ids = {j["id"] for j in jobs}
    leftover = [j for j in leftover if j["id"] not in planned_ids]
    if leftover:
        log(f"+ недобитых окон с прошлого запуска: {len(leftover)} (--no-resume — бросить их)")
        jobs += leftover
    log(
        f"тегов {len(tags)}, окон {len(jobs)}, воркеров {workers}, {describe_pool(pool)}, "
        f"период {fmt_ts(args.start_time)} – {fmt_ts(args.end_time or time.time())}, "
        f"по {args.count} постов/запрос"
    )

    left = 0
    started = time.time()
    for round_no in range(1, passes + 1):
        if round_no > 1:
            log(f"раунд {round_no - 1} готов, пауза {repeat_pause} с до прохода за новыми постами")
            if not sleep_interruptible(repeat_pause):
                break
            jobs = plan_round(db, tags, args, settings, round_no)
            log(f"раунд {round_no}: окон {len(jobs)} (только свежие посты)")
        left = run_round(jobs, pool, workers, args, settings, db)
        if _stop_flag.is_set() or left:
            if left:
                log(f"живых аккаунтов не осталось, окон в очереди: {left} — запуск позже их добьёт")
            break

    finish_tags(db, tags, args)
    stats = snapshot()
    elapsed = max(1.0, time.time() - started)
    print("─" * 60)
    log(
        f"итого за {elapsed / 60:.1f} мин: окон {stats['jobs']} (делений {stats['split']}), "
        f"страниц {stats['pages']}, новых постов {stats['new']} "
        f"({stats['new'] / elapsed * 60:.0f}/мин), ошибок {stats['errors']} "
        f"(капча {stats['captcha']}, токен {stats['token']}, пустых выдач {stats['throttle']})"
    )
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    if not args.no_export and not args.dry_run:
        try:
            path = EXPORTS_DIR / f"vk_feed_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            export_posts(db, path, tag=args.tag_filter or None)
        except Exception as exc:  # noqa: BLE001
            log(f"финальный экспорт не удался: {exc}")
    return 0 if left == 0 and not _stop_flag.is_set() else 2


if __name__ == "__main__":
    raise SystemExit(main())
