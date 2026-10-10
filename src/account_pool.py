#!/usr/bin/env python3
"""Пул аккаунтов VK: проверка, статусы и ротация для многопоточного сбора.

Воркеры не закреплены за аккаунтами: каждый берёт из пула свободный, а когда
аккаунт уходит в отлёжку (капча, пустые выдачи, лимиты) или выбывает (нужен
повторный вход) — берёт следующий живой. Так 5 потоков крутят 20 аккаунтов,
а не первые 5.

Статусы аккаунта: ready — готов, можно брать; hold — отлёжка до cooldown_until
(капча, flood, пустые выдачи); dead — выбыл из прогона (нет сессии или нужен
повторный вход).

Запуск:
    python src/account_pool.py --status     # кто работает, в отлёжке, мёртв
    python src/account_pool.py --validate   # проверить все аккаунты

validate_accounts проверяет все аккаунты сразу при старте, независимо от числа
рабочих потоков, и пишет статус в базу.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import Account, load_accounts, resolve_proxy  # noqa: E402
from state_db import DB  # noqa: E402

READY = "ready"
HOLD = "hold"
DEAD = "dead"

# Нарастающая отлёжка (сек). Короткая капча/лимит отпускает быстро,
# затяжной flood — надолго.
HOLD_PAUSES = (60, 300, 900, 1800, 3600)

# Как читать статусы прошлых прогонов при загрузке пула.
_DEAD_STATUSES = {"dead", "token", "no_session", "banned"}
_HOLD_STATUSES = {"hold", "throttled", "captcha", "limit", "flood"}

# Человеческие названия статусов (окно массового запуска и CLI --status).
STATUS_LABELS = {READY: "работает", HOLD: "отлёжка", DEAD: "мёртв"}


def normalize_status(raw: str, cooldown_until: float = 0.0, now: Optional[float] = None) -> str:
    """Любой статус из базы, в том числе старый: ready, hold или dead."""
    return _normalize_status(raw, float(cooldown_until or 0), time.time() if now is None else now)


def status_label(status: str) -> str:
    """Подпись статуса для интерфейса: ready — «работает»."""
    return STATUS_LABELS.get(status, status)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class PoolAccount:
    """Аккаунт в пуле: логин, прокси, файл сессии и текущее состояние."""

    login: str
    proxy: str = ""
    session_path: Optional[Path] = None
    status: str = READY
    cooldown_until: float = 0.0
    fail_count: int = 0
    posts: int = 0
    user_id: int = 0
    busy: bool = False
    reason: str = ""

    @property
    def is_ready(self) -> bool:
        return self.status == READY or (self.status == HOLD and time.time() >= self.cooldown_until)


def _normalize_status(raw: str, cooldown_until: float, now: float) -> str:
    s = (raw or READY).strip().lower()
    if s in _DEAD_STATUSES:
        return DEAD
    if s in _HOLD_STATUSES:
        return READY if cooldown_until <= now else HOLD
    return READY


def load_pool_accounts(
    accounts: Iterable[Account], db: Optional[DB], proxy_override: str = ""
) -> list[PoolAccount]:
    """Собирает пул из accounts.txt, накладывая статусы из базы."""
    now = time.time()
    items: list[PoolAccount] = []
    for acc in accounts:
        state = db.account_state(acc.login) if db else {}
        cooldown = float(state.get("cooldown_until") or 0)
        items.append(
            PoolAccount(
                login=acc.login,
                proxy=proxy_override or resolve_proxy(acc),
                session_path=acc.session_path,
                status=_normalize_status(state.get("status") or READY, cooldown, now),
                cooldown_until=cooldown if cooldown > now else 0.0,
                fail_count=int(state.get("fail_count") or 0),
                posts=int(state.get("total_items") or 0),
                user_id=int(state.get("user_id") or 0),
            )
        )
    return items


class AccountPool:
    """Набор аккаунтов с ротацией.

    acquire() берёт свободный здоровый аккаунт (или ждёт, пока отлёжка кончится).
    release(outcome) возвращает его в пул: ok — готов, hold — отлёжка с нарастающей
    паузой, dead — выбыл из прогона.
    """

    def __init__(self, items: list[PoolAccount]) -> None:
        self._items = list(items)
        self._cv = threading.Condition()
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._items)

    def alive(self) -> int:
        return sum(1 for a in self._items if a.status != DEAD)

    def _pick(self, now: float) -> Optional[PoolAccount]:
        n = len(self._items)
        if not n:
            return None
        for step in range(n):
            acc = self._items[(self._cursor + step) % n]
            if acc.busy or acc.status == DEAD:
                continue
            if acc.status == READY or (acc.status == HOLD and now >= acc.cooldown_until):
                return acc
        return None

    def _next_wait(self, now: float) -> Optional[float]:
        holds = [
            a.cooldown_until - now
            for a in self._items
            if a.status == HOLD and not a.busy and a.cooldown_until > now
        ]
        if holds:
            return max(0.2, min(holds))
        if any(a.busy for a in self._items):
            return 0.5  # ждём, пока кто-то освободит аккаунт
        return None

    def acquire(self, timeout: Optional[float] = None) -> Optional[PoolAccount]:
        """Берёт аккаунт из пула; None — здоровых не осталось."""
        with self._cv:
            deadline = None if timeout is None else time.time() + timeout
            while True:
                now = time.time()
                acc = self._pick(now)
                if acc is not None:
                    acc.busy = True
                    acc.reason = ""
                    if acc.status == HOLD:
                        acc.status = READY
                        acc.cooldown_until = 0.0
                    self._cursor = (self._items.index(acc) + 1) % max(1, len(self._items))
                    self._cv.notify_all()
                    return acc
                if self.alive() == 0:
                    return None
                if deadline is not None and now >= deadline:
                    return None
                wait = self._next_wait(now)
                if wait is None:
                    return None
                if deadline is not None:
                    wait = max(0.01, min(wait, deadline - now))  # не пересиживаем дедлайн
                self._cv.wait(timeout=wait)

    def release(self, acc: PoolAccount, outcome: str = "ok", reason: str = "") -> float:
        """Возвращает аккаунт в пул и его cooldown_until (для hold)."""
        with self._cv:
            acc.busy = False
            acc.reason = reason
            if outcome == "ok":
                acc.status = READY
                acc.cooldown_until = 0.0
                acc.fail_count = 0
                self._cv.notify_all()
                return 0.0
            if outcome == "dead":
                acc.status = DEAD
                self._cv.notify_all()
                return 0.0
            acc.fail_count += 1
            idx = min(acc.fail_count - 1, len(HOLD_PAUSES) - 1)
            acc.status = HOLD
            acc.cooldown_until = time.time() + HOLD_PAUSES[idx]
            self._cv.notify_all()
            return acc.cooldown_until

    def add_posts(self, acc: PoolAccount, count: int) -> None:
        with self._cv:
            acc.posts += int(count or 0)

    def snapshot(self) -> list[dict]:
        with self._cv:
            now = time.time()
            return [
                {
                    "login": a.login,
                    "status": a.status,
                    "busy": a.busy,
                    "proxy": a.proxy,
                    "posts": a.posts,
                    "fail_count": a.fail_count,
                    "cooldown_left": int(max(0.0, a.cooldown_until - now)) if a.status == HOLD else 0,
                    "reason": a.reason,
                }
                for a in self._items
            ]



# Проверка аккаунтов (независимо от рабочих потоков)
def check_account(
    acc: Account,
    proxy: str = "",
    *,
    tag: str = "",
    use_browser: bool = True,
) -> dict:
    """Проверяет один аккаунт: сессия, токен, users.get, при желании поиск.

    Возвращает словарь с полями login, status, reason, proxy, user_id, profile.
    """
    from vk_auth import (  # локальный импорт: без циклов и без Playwright на старте
        VkApiError,
        load_session,
        refresh_from_cookies,
        refresh_via_browser,
        token_alive,
        validate_token,
    )

    login = acc.login
    proxy = proxy or resolve_proxy(acc)
    session_path = acc.session_path
    out = {"login": login, "status": READY, "reason": "", "proxy": proxy, "user_id": 0, "profile": ""}

    if not Path(session_path).exists():
        return {**out, "status": DEAD, "reason": "нет файла сессии — нужен вход"}

    data = load_session(session_path) or {}
    if not data:
        return {**out, "status": DEAD, "reason": "сессия пуста/бита"}

    if not token_alive(data):
        attempts: list[tuple[str, Callable[[Path, str], dict]]] = []
        if use_browser:
            attempts.append(("профиль браузера", refresh_via_browser))
        attempts.append(("cookies", refresh_from_cookies))
        last = "нет способов обновления"
        for name, fn in attempts:
            try:
                fn(session_path, proxy)
                last = ""
                break
            except Exception as exc:  # noqa: BLE001
                last = f"{name}: {exc}"
        if last:
            return {**out, "status": DEAD, "reason": f"токен не обновлён ({last})"}
        data = load_session(session_path) or {}

    token = str(data.get("access_token") or "")
    profile: dict = {}
    try:
        profile = validate_token(token, proxy)
    except VkApiError as exc:  # код 5/15/27/28 — токен неверный/отозванный → мёртв
        code = int(getattr(exc, "code", 0) or 0)
        status = DEAD if code in (5, 15, 27, 28) else HOLD
        return {**out, "status": status, "reason": f"users.get: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {**out, "status": HOLD, "reason": f"users.get: {exc}"}

    if tag:
        try:
            from vk_client import VkFeedClient

            client = VkFeedClient(token, proxy, login=login)
            client.search_hashtag(tag, count=3)
        except Exception as exc:  # noqa: BLE001
            return {**out, "status": HOLD, "reason": f"поиск не прошёл: {exc}"}

    user = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    return {
        **out,
        "status": READY,
        "reason": user or "ок",
        "user_id": int(profile.get("id") or data.get("user_id") or 0),
        "profile": user,
    }



def validate_accounts(
    db: Optional[DB] = None,
    accounts: Optional[list[Account]] = None,
    *,
    workers: int = 4,
    tag: str = "",
    proxy_override: str = "",
    use_browser: bool = True,
    on_result: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Проверяет ВСЕ аккаунты сразу (не глядя на рабочие потоки) и пишет статус в БД."""
    import concurrent.futures as futures

    db = db if db is not None else DB()
    accounts = list(accounts) if accounts is not None else load_accounts()
    summary = {"total": len(accounts), "ready": 0, "hold": 0, "dead": 0, "results": []}
    if not accounts:
        return summary

    def work(acc: Account) -> dict:
        return check_account(
            acc,
            proxy_override or resolve_proxy(acc),
            tag=tag,
            use_browser=use_browser,
        )

    limit = max(1, min(int(workers or 4), len(accounts)))
    with futures.ThreadPoolExecutor(max_workers=limit, thread_name_prefix="check") as executor:
        for res in executor.map(work, accounts):
            summary[res["status"]] = summary.get(res["status"], 0) + 1
            summary["results"].append(res)
            if db is not None:
                db.mark_account(
                    res["login"],
                    user_id=res.get("user_id") or None,
                    proxy=res.get("proxy") or "",
                    status=res["status"],
                    error="" if res["status"] == READY else res["reason"],
                    touch_ok=res["status"] == READY,
                )
            if on_result:
                on_result(res)
    return summary


def _print_status(db: DB) -> None:
    rows = db.list_accounts()
    if not rows:
        log("в базе нет аккаунтов — сначала добавьте их в data/accounts.txt")
        return
    now = time.time()
    log(f"{'логин':<28} {'статус':<8} {'постов':>7} {'сбоев':>6}  детали")
    for r in rows:
        status = _normalize_status(r.get("status") or READY, float(r.get("cooldown_until") or 0), now)
        left = ""
        if status == HOLD and float(r.get("cooldown_until") or 0) > now:
            left = f", отлёжка ещё {int(float(r['cooldown_until']) - now)} с"
        detail = (r.get("last_error") or "").strip()
        log(
            f"{str(r.get('login')):<28} {STATUS_LABELS[status]:<8} "
            f"{int(r.get('total_items') or 0):>7} "
            f"{int(r.get('fail_count') or 0):>6}  {detail}{left}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Пул аккаунтов VK: проверка и статусы")
    parser.add_argument("--validate", action="store_true", help="проверить все аккаунты")
    parser.add_argument("--workers", type=int, default=4, help="сколько проверять одновременно")
    parser.add_argument("--tag", default="", help="тестовый поиск по тегу при проверке")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер при обновлении токена")
    parser.add_argument("--status", action="store_true", help="показать статусы из базы")
    args = parser.parse_args(argv)

    db = DB()
    if not args.validate:
        _print_status(db)
        return 0

    summary = validate_accounts(
        db,
        workers=args.workers,
        tag=args.tag,
        use_browser=not args.no_browser,
        on_result=lambda r: log(f"{r['login']}: {r['status']} — {r['reason']}"),
    )
    print(json.dumps({k: summary[k] for k in ("total", "ready", "hold", "dead")}, ensure_ascii=False))
    return 0 if summary["dead"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

