#!/usr/bin/env python3
"""Панель массового запуска в браузере: аккаунты, прогон, теги, события, лог.

Запуск:
    python src/webui.py                 # поднять панель и открыть браузер
    python src/webui.py --port 9000     # другой порт
    python src/webui.py --host 0.0.0.0  # сервер: панель доступна по сети (у неё НЕТ пароля)
    python src/webui.py --no-browser    # не открывать браузер
    python src/webui.py --selftest      # проверить API и выйти

Сервер по умолчанию слушает только 127.0.0.1 — снаружи панель недоступна. На сервере
(Windows или Linux) удобнее смотреть её через SSH-туннель:
    ssh -L 8787:127.0.0.1:8787 user@server   →  http://127.0.0.1:8787
--host 0.0.0.0 нужен, только если панель открывают прямо по сети: пароля у неё нет,
поэтому порт закрывают фаерволом или обратным прокси. На сервере без графики браузер
не открывается сам (это нормально) — адрес печатается в окне запуска.

Прогон идёт отдельным процессом (src/run_tags.py), «Стоп» посылает ему Ctrl+C,
состояние панель читает из data/parser.sqlite3. Сборка не нужна: чистый Python и один
HTML-файл рядом.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from account_pool import DEAD, HOLD, NEEDS_PROXY, READY, normalize_status, status_label  # noqa: E402
from proxy_stock import add_proxy_batch, bind_free_proxies  # noqa: E402
from accounts import (  # noqa: E402
    account_key,
    compact_accounts_file,
    delete_accounts,
    load_accounts,
    parse_account_line,
    proxies_from_file,
    resolve_proxy,
    upsert_accounts,
)
from gui import (  # noqa: E402
    EVENT_LEVELS,
    PERIODS,
    TAG_LABELS,
    build_run_args,
    cooldown_text,
    hhmmss,
    open_path,
    parse_tags_text,
    tags_in_file,
)
from paths import (  # noqa: E402
    ACCOUNTS_FILE,
    BASE_DIR,
    DATA_DIR,
    EXPORTS_DIR,
    PROFILE_DIR,
    PROXIES_FILE,
    TAGS_FILE,
    ensure_layout,
    invoke_cmd,
    resolve_proxies_file,
)
from settings import (  # noqa: E402
    TAG_QUERY_LABELS,
    load_settings,
    save_settings,
    tag_query_value,
)
from state_db import DB  # noqa: E402
from platform_compat import (  # noqa: E402
    can_open_browser,
    os_label,
)

WEB_DIR = SRC_DIR / "webui"
HOST = "127.0.0.1"  # по умолчанию панель видна только с этого компьютера
DEFAULT_PORT = 8787
LOG_TAIL = 4000  # сколько строк лога держим в буфере

_panel_db: Optional[DB] = None
_panel_db_lock = threading.Lock()


def panel_db() -> DB:
    """Одно соединение панели на процесс (DB внутри держит своё на поток).

    Раньше каждый запрос панели создавал новый DB() и новое соединение: лишние
    блокировки базы и «OperationalError: database is locked» у самого прогона.
    """
    global _panel_db
    if _panel_db is None:
        with _panel_db_lock:
            if _panel_db is None:
                _panel_db = DB()
    return _panel_db

# Файлы, которые панель показывает и сохраняет.
EDITABLE = {
    "tags": TAGS_FILE,
    "accounts": ACCOUNTS_FILE,
    "proxies": PROXIES_FILE,
}

# Процесс прогона
RUN_LABEL = "прогон"  # метка процесса сбора: по ней «автовход» понимает, что встало


class Runner:
    """Один процесс за раз: запуск, поток чтения вывода, буфер строк для лога."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.label = ""
        self.started_at = 0.0
        self._lines: deque = deque(maxlen=LOG_TAIL)
        self._seq = 0

    # лог
    def log(self, msg: str) -> None:
        self._push(f"[{time.strftime('%H:%M:%S')}] панель: {msg}")

    def _push(self, line: str) -> None:
        with self._lock:
            self._seq += 1
            self._lines.append((self._seq, line))

    def tail(self, after: int) -> tuple[list[str], int]:
        """Строки после номера after: панель забирает только новое."""
        with self._lock:
            fresh = [line for seq, line in self._lines if seq > after]
            return fresh, self._seq

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def state_text(self) -> str:
        if self.running:
            minutes = (time.time() - self.started_at) / 60.0
            return f"идёт: {self.label} · {minutes:.1f} мин"
        return "ожидание"

    # запуск
    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        kwargs: dict = {
            "cwd": str(BASE_DIR),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":  # чтобы «Стоп» умел послать Ctrl+Break
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(cmd, **kwargs)

    def start(self, script: str, extra: list[str], label: str) -> tuple[bool, str]:
        """Запускает модуль из src/ отдельным процессом и льёт его вывод в лог."""
        return self.spawn(invoke_cmd(script) + list(extra), label)

    def spawn(self, cmd: list, label: str) -> tuple[bool, str]:
        if self.running:
            return False, "уже идёт другой процесс — дождитесь конца или нажмите «Стоп»"
        self.log("$ " + " ".join(str(part) for part in cmd))
        try:
            proc = self._popen([str(part) for part in cmd])
        except Exception as exc:  # noqa: BLE001
            message = f"не удалось запустить {label}: {exc}"
            self.log(message)
            return False, message
        self.proc = proc
        self.label = label
        self.started_at = time.time()
        threading.Thread(target=self._reader, args=(proc, label), daemon=True).start()
        return True, ""

    def _reader(self, proc: subprocess.Popen, label: str) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._push(line.rstrip("\n"))
        except Exception as exc:  # noqa: BLE001
            self.log(f"чтение вывода прервалось: {exc}")
        code = proc.wait()
        if self.proc is proc:
            self.proc = None
        self.log(f"{label}: процесс завершён (код {code})")
        try:
            autologin_after_failure(self, label, code)
        except Exception as exc:  # noqa: BLE001 — панель не падает из-за автовхода
            self.log(f"автовход: не сработал ({exc})")

    def stop(self) -> tuple[bool, str]:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False, "нечего останавливать: процесс не запущен"
        self.log(f"останавливаю {self.label} — прогресс сохраняется…")
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception as exc:  # noqa: BLE001
            self.log(f"не получилось послать Ctrl+C ({exc}) — завершаю процесс")
            proc.terminate()
        threading.Thread(target=self._kill_later, args=(proc,), daemon=True).start()
        return True, ""

    def _kill_later(self, proc: subprocess.Popen, delay: float = 7.5) -> None:
        time.sleep(delay)
        if proc.poll() is None:
            self.log("процесс не ответил на Ctrl+C — завершаю принудительно")
            proc.terminate()


# Что показываем панели
def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def login_batch_payload() -> dict:
    """Состояние очереди входов: панель рисует по нему кнопку «Войти во все, кому нужен вход»."""
    return dict(_login_batch)


def status_payload(runner: Runner, port: int) -> dict:
    return {
        "running": runner.running,
        "label": runner.label,
        "state": runner.state_text(),
        "login_batch": login_batch_payload(),
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "exports_dir": str(EXPORTS_DIR),
        "files": {kind: str(path) for kind, path in EDITABLE.items()},
        "periods": [{"label": label, "hours": hours} for label, hours in PERIODS],
        "events": EVENT_LEVELS,
        "port": port,
    }


def accounts_summary(db_rows: Optional[list[dict]] = None) -> dict:
    """Сколько аккаунтов в работе: считаем по accounts.txt, а не по базе.

    В базе остаются строки удалённых аккаунтов (и раньше — двойники «+7…»), из-за
    чего карточка «Аккаунты» показывала больше, чем есть на самом деле: файл —
    источник списка, база — только статусы.
    """
    now = time.time()
    accounts = load_accounts(ACCOUNTS_FILE)
    rows = panel_db().list_accounts() if db_rows is None else db_rows
    db_by_key: dict[str, dict] = {}
    for row in rows:
        login = str(row.get("login") or "")
        if login:
            db_by_key[account_key(login)] = row
    counts = {READY: 0, HOLD: 0, DEAD: 0, NEEDS_PROXY: 0}
    for acc in accounts:
        row = db_by_key.get(account_key(acc.login))
        if row:
            status = normalize_status(
                str(row.get("status") or READY), float(row.get("cooldown_until") or 0), now
            )
        elif acc.session_path.exists():
            status = READY
        else:
            status = DEAD
        if not resolve_proxy(acc):
            status = NEEDS_PROXY  # без прокси в работу не берём
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(accounts),
        "ready": counts[READY],
        "hold": counts[HOLD],
        "dead": counts[DEAD],
        "needs_proxy": counts[NEEDS_PROXY],
        "db_rows": len(db_by_key),
    }


def progress_payload(runner: Runner) -> dict:
    db = panel_db()
    stats = db.stats()
    accounts = accounts_summary()
    proxies = proxies_from_file(resolve_proxies_file())
    total, finished = int(stats["tags_total"]), int(stats["tags_finished"])
    return {
        "running": runner.running,
        "state": runner.state_text(),
        "login_batch": login_batch_payload(),
        "posts_total": int(stats["posts"]),
        "posts_last_hour": int(stats["posts_last_hour"]),
        "tag_links": int(stats["tag_links"]),
        # Карточка «Постов в базе» считает с последнего «Обнулить счётчики»
        # (state_db.counter_mark): posts_total/tag_links — всё, что лежит в базе,
        # «_since_reset» — сколько собрано после обнуления (карточка и подпись).
        "posts_since_reset": int(stats["posts_since_reset"]),
        "tag_links_since_reset": int(stats["tag_links_since_reset"]),
        "counter_reset_at": float(stats["counter_reset_at"]),
        "tags_total": total,
        "tags_finished": finished,
        "tags_pct": round(100.0 * finished / total, 1) if total else 0.0,
        "accounts_total": accounts["total"],
        "accounts_ready": accounts["ready"],
        "accounts_hold": accounts["hold"],
        "accounts_dead": accounts["dead"],
        "accounts_needs_proxy": accounts["needs_proxy"],
        "accounts_db": accounts["db_rows"],
        "accounts_file": accounts["total"],
        "proxies_total": len(proxies),
    }


def accounts_view() -> list[dict]:
    """Таблица аккаунтов из accounts.txt + статусы из БД (файл — источник списка).

    У строки есть needs_login: вход нужен, если файла сессии нет или сессия мертва
    (VK ответил «access_token has expired») — по этому флагу кнопка «Войти во все,
    кому нужен вход» собирает очередь. Раньше очередь брала только строки без файла
    сессии, поэтому протухшие аккаунты в неё не попадали, хотя вход нужен именно им.
    """
    now = time.time()
    compact_accounts_file(ACCOUNTS_FILE)  # если раньше накопились дубли — подчистим
    accounts = load_accounts(ACCOUNTS_FILE)
    db_by_key: dict[str, dict] = {}
    for row in panel_db().list_accounts():
        login = str(row.get("login") or "")
        if login:
            db_by_key[account_key(login)] = row

    out = []
    for number, acc in enumerate(accounts, 1):
        row = db_by_key.pop(account_key(acc.login), {})
        has_session = acc.session_path.exists()
        if row:
            status = normalize_status(
                str(row.get("status") or READY), float(row.get("cooldown_until") or 0), now
            )
            error = (row.get("last_error") or "").strip()
        elif has_session:
            status = READY
            error = "добавлен — нажмите «Проверить аккаунты»"
        else:
            status = DEAD
            error = "нет файла сессии — нужен вход"
        # Вход нужен, если сессии нет или она мертва (протух токен). Статус «нужен
        # прокси» к этому не относится: там лечит прокси, а не вход, поэтому флаг
        # считается до подмены статуса.
        needs_login = (not has_session) or status == DEAD
        if not resolve_proxy(acc):
            status = NEEDS_PROXY
            error = "нужен прокси — закрепите адрес в accounts.txt"
        out.append(
            {
                "login": acc.login,
                "number": number,
                "status": status,
                "status_label": status_label(status),
                "proxy": resolve_proxy(acc),
                "needs_proxy": not resolve_proxy(acc),
                "shared_with": [],
                "posts": int(row.get("total_items") or 0),
                "fails": int(row.get("fail_count") or 0),
                "cooldown": cooldown_text(row, now) if row else "—",
                "error": error,
                "session": has_session,
                "needs_login": needs_login,
            }
        )
    from accounts import proxy_sharing as _sharing

    shared = _sharing(accounts, {})
    for item in out:
        proxy = item["proxy"]
        if proxy in shared:
            item["shared_with"] = [x for x in shared[proxy] if x != item["login"]]
    return sorted(out, key=lambda item: (item["status"] != READY, item["login"]))


def tags_view() -> list[dict]:
    db = panel_db()
    out = []
    for row in db.all_tags():
        query = str(row.get("query") or "")
        status = str(row.get("status") or "")
        out.append(
            {
                "query": query,
                "status": status,
                "status_label": TAG_LABELS.get(status, status),
                "items": int(row.get("total_items") or 0),
                "pages": int(row.get("total_pages") or 0),
                "open_jobs": db.count_open_jobs(query),
                "updated": hhmmss(float(row.get("updated_at") or 0)),
            }
        )
    return out


def events_view(limit: int = 40) -> list[dict]:
    out = []
    for event in panel_db().recent_events(limit):
        level = str(event.get("level") or "")
        out.append(
            {
                "time": hhmmss(float(event.get("created_at") or 0)),
                "level": level,
                "level_label": EVENT_LEVELS.get(level, level),
                "kind": event.get("kind") or "",
                "account": event.get("account") or "",
                "tag": event.get("tag") or "",
                "message": event.get("message") or "",
            }
        )
    return out


# Действия панели
def action_run(runner: Runner, body: dict) -> tuple[bool, str]:
    """Старт прогона: те же аргументы, что у окна (src/gui.py)."""
    settings = load_settings()
    workers = max(1, int(body.get("workers") or settings.get("max_accounts", 2)))
    passes = max(1, int(body.get("passes") or settings.get("repeat_passes", 1)))
    hours = float(body.get("hours") or 0.0)
    # «Работать, ч» (--run-hours): период — это ЧТО собирать, а сколько работать
    # задаёт это поле, поэтому число проходов его не ограничивает (иначе время
    # работы кончилось бы на первом же проходе).
    duty_hours = max(0.0, float(body.get("run_hours") or 0.0))
    pause = max(0, int(body.get("repeat_pause") or settings.get("repeat_pause_sec", 30)))
    tags_file = Path(str(body.get("tags_file") or TAGS_FILE)).expanduser()
    # Эмулятор браузера на виду: видно, как он обновляет ленту через F5. Галочка
    # на странице «Прогон» — она же сохраняется в show_browser.
    show_browser = bool(body.get("show_browser", settings.get("show_browser", False)))
    # Чем искать тег в запросе к VK: «с #» (хештег) или «без #» (слово). Переключатель
    # стоит на странице «Теги», а прогон получает режим из настроек — он один на весь
    # прогон. Если панель прислала значение вместе со стартом, берём его.
    tag_query = tag_query_value(str(body.get("tag_query") or settings.get("tag_query") or ""))

    settings["max_accounts"] = workers
    settings["repeat_passes"] = passes
    settings["show_browser"] = show_browser
    settings["tag_query"] = tag_query
    if duty_hours <= 0:
        settings["repeat_pause_sec"] = pause
    try:
        save_settings(settings)
    except OSError as exc:
        runner.log(f"настройки не сохранились: {exc}")

    args = build_run_args(
        hours=hours,
        passes=1 if duty_hours > 0 else passes,
        workers=workers,
        validate_first=bool(body.get("validate_first", True)),
        full=bool(body.get("full", False)),
        no_refresh=bool(body.get("no_refresh", False)),
        dry_run=bool(body.get("dry_run", False)),
        run_hours=duty_hours,
        repeat_pause=pause,
        tags_file=tags_file,
        show_browser=show_browser,
        tag_query=tag_query,
    )
    ok, message = runner.start("run_tags", args, RUN_LABEL)
    if ok:
        if duty_hours > 0:
            message = (
                f"работаю {duty_hours:g} ч от «Старт»: непрерывно собираю новые посты "
                f"(«Стоп» — прервать)"
            )
        if show_browser:
            message += (
                " · окно браузера-эмулятора показываю: откроется, когда сессию надо "
                "будет освежить через ленту (F5)"
            )
        if tag_query == "word":
            message += " · тег ищу словом, без решётки — это другая выдача, чем #тег"
        elif tag_query == "auto":
            message += (
                " · режим тега выберу сам: для каждого тега — хештег или слово, "
                "где выдача больше"
            )
    return ok, message or "прогон запущен"


def action_add_tags(body: dict) -> tuple[bool, str]:
    tags, bad = parse_tags_text(str(body.get("content") or ""))
    if not tags:
        return False, bad[0] if bad else "введите хотя бы один тег"
    path = Path(str(body.get("tags_file") or TAGS_FILE)).expanduser()
    already = tags_in_file(path)
    fresh = [tag for tag in tags if tag.lower() not in already]
    if not fresh:
        return True, f"{path.name}: теги уже в файле — {', '.join(tags)}"
    try:
        with path.open("a", encoding="utf-8") as fh:
            for tag in fresh:
                fh.write(tag + "\n")
    except OSError as exc:
        return False, f"не удалось записать {path.name}: {exc}"
    skipped = [tag for tag in tags if tag not in fresh]
    note = f" (уже было: {', '.join(skipped)})" if skipped else ""
    return True, f"{path.name}: добавлено тегов — {', '.join(fresh)}{note}"


def action_reset_tag(body: dict) -> tuple[bool, str]:
    tag = str(body.get("query") or "").strip()
    if not tag:
        return False, "тег не выбран"
    try:
        count = panel_db().reset_tags([tag])
    except Exception as exc:  # noqa: BLE001
        return False, f"не получилось сбросить «{tag}»: {exc}"
    return True, f"тег «{tag}» вернулся в очередь (сброшено записей: {count})"


def action_reset_tag_stats(body: dict) -> tuple[bool, str]:
    """Обнуляет счётчики тега (Постов/Страниц/Проходов) — «В очередь» их не сбрасывает.

    «В очередь» возвращает тег к сбору (статус и курсор), а накопленные цифры остаются
    историей за всё время — они только растут (bump_tag). Здесь наоборот: обнуляются
    только счётчики, статус и курсор тега не трогаются.

    Заодно сдвигается отсчёт карточки «Постов в базе» (state_db.counter_mark): цифры
    начинаются с нуля, а сами посты остаются в базе — «всего в базе N» в подписи.
    """
    tag = str(body.get("query") or "").strip()
    if not tag:
        return False, "тег не выбран"
    try:
        count = panel_db().reset_tag_stats([tag])
    except Exception as exc:  # noqa: BLE001
        return False, f"не получилось обнулить счётчики «{tag}»: {exc}"
    return True, (
        f"счётчики тега «{tag}» обнулены (записей: {count}) — статус и курсор не тронуты,"
        " карточка «Постов в базе» считает с нуля (посты из базы не удалены)"
    )


def action_add_proxies(body: dict) -> tuple[bool, str]:
    """Пачка прокси: добавляет в запас, дубли и мусор пропускает, отчёт — в сообщении."""
    res = add_proxy_batch(str(body.get("content") or ""))
    if res["parsed"] == 0:
        bad = "; ".join(f"«{s}» — {why}" for s, why in res["invalid"][:5])
        return False, "ни одного прокси не распознано" + (f": {bad}" if bad else "")
    msg = (f"прокси распознано {res['parsed']}, добавлено {res['added']}, "
           f"уже были {res['duplicates']}, в запасе всего {res['stock']}")
    if res["invalid"]:
        msg += f"; пропущено строк: {len(res['invalid'])}"
    return True, msg


def action_bind_proxies() -> tuple[bool, str]:
    """Привязывает свободные прокси к аккаунтам без своего — по одному на аккаунт."""
    res = bind_free_proxies()
    msg = f"привязано {res['bound']}, без прокси остаётся {res['without_proxy']}, в запасе {res['stock_left']}"
    if res["without_proxy"]:
        msg += " — докупите прокси"
    return True, msg


def action_add_accounts(body: dict) -> tuple[bool, str]:
    lines = [line.strip() for line in str(body.get("content") or "").splitlines()]
    keep = [
        line
        for line in lines
        if line and not line.startswith("#") and not line.startswith("//")
    ]
    accounts = [acc for acc in (parse_account_line(line, 0) for line in keep) if acc]
    if not accounts:
        return False, (
            "ни одной строки не разобрал — нужен формат "
            "телефон;пароль;2FA;прокси или LOGIN:PASS:TOKEN"
        )
    try:
        added, updated, unchanged = upsert_accounts(ACCOUNTS_FILE, accounts)
    except OSError as exc:
        return False, f"не удалось записать {ACCOUNTS_FILE.name}: {exc}"
    total = len(load_accounts(ACCOUNTS_FILE))
    parts = []
    if added:
        parts.append(f"новых {added}")
    if updated:
        parts.append(f"обновлено {updated}")
    if unchanged:
        parts.append(f"уже были {unchanged}")
    detail = ", ".join(parts) if parts else "без изменений"
    return True, f"{ACCOUNTS_FILE.name}: {detail}, всего уникальных {total}"


def account_profile_dir(account, number: int) -> Path:
    """Профиль Chromium этого аккаунта: из его сессии, иначе свой по номеру.

    У каждого аккаунта профиль отдельный — иначе входы перетирают друг друга.
    Если аккаунт уже входил, берём папку из data/session_<логин>.json
    (парсер сам обновляет токен по этому же профилю).
    """
    session = account.session_path
    recorded = ""
    if session.exists():
        try:
            saved = json.loads(session.read_text(encoding="utf-8"))
            recorded = str(saved.get("profile_dir") or "").strip()
        except (OSError, ValueError):
            recorded = ""
    if recorded:
        return Path(recorded)
    return PROFILE_DIR.parent / f"{PROFILE_DIR.name}_{number}"


LOGIN_GAP_SEC = 30  # пауза между запусками входа: не чаще одного в полминуты
LOGIN_BATCH_GAP_SEC = 8  # пауза между окнами очереди «войти во все, кому нужен вход»
LOGIN_BATCH_TIMEOUT_SEC = 450  # сколько ждём человека в одном окне (как --timeout у vk_auth)
_last_login = {"t": 0.0}

# Очередь «войти во все, кому нужен вход» (нет сессии или протух токен): отдельный
# поток открывает окна по одному, телефон/пароль/капчу в каждом окне решает человек.
# Панель рисует по этому состоянию кнопку.
_login_batch = {
    "active": False,
    "cancel": False,
    "done": 0,
    "failed": 0,
    "total": 0,
    "login": "",
}


def action_delete_accounts(runner: Runner, body: dict) -> tuple[bool, str]:
    """Удаляет аккаунты: из accounts.txt, из базы и их файлы сессий.

    body: {"logins": [...]} — выбранные; {"mode": "dead"} — все мёртвые;
    {"logins": [один]} — одна строка из таблицы.
    Пока работает любой процесс (проверка, сбор, вход), удаление запрещено:
    процесс держит аккаунт и перезапишет базу после себя.
    """
    if runner.running:
        return False, "сейчас идёт процесс — дождитесь окончания или нажмите «Стоп», потом удаляйте"
    if str(body.get("mode") or "") == "dead":
        logins = [row["login"] for row in accounts_view() if row["status"] == DEAD]
    else:
        logins = [str(x).strip() for x in (body.get("logins") or []) if str(x).strip()]
    if not logins:
        return False, "никого не выбрано"
    removed, backup = delete_accounts(logins, ACCOUNTS_FILE)
    db_rows = panel_db().delete_accounts(logins)
    sessions = 0
    for login in logins:
        path = _session_path(login)
        if path.exists():
            try:
                path.unlink()
                sessions += 1
            except OSError:
                pass
    return True, (
        f"удалено из accounts.txt: {removed}, из базы: {db_rows}, файлов сессий: {sessions}. "
        f"Бэкап: {backup.name}"
    )


def _session_path(login: str) -> Path:
    from accounts import Account

    return Account(login=login).session_path


def _wait_login_gap(gap: float) -> bool:
    """Ждёт остаток паузы между входами. False — очередь остановили кнопкой."""
    while not _login_batch["cancel"]:
        wait = gap + 0.5 - (time.time() - _last_login["t"])
        if wait <= 0:
            return True
        time.sleep(min(0.5, wait))
    return False


def _start_login(
    runner: Runner, number: int, qr: bool = False, gap: float = LOGIN_GAP_SEC
) -> tuple[bool, str]:
    """Запускает vk_auth.py --login для аккаунта №number из accounts.txt (строки с единицы)."""
    accounts = load_accounts(ACCOUNTS_FILE)
    if not accounts:
        return False, "нет аккаунтов: добавьте строки в accounts.txt"
    if not 1 <= number <= len(accounts):
        return False, f"аккаунта №{number or '—'} нет в accounts.txt (там их {len(accounts)})"
    account = accounts[number - 1]
    login = account.name
    extra = [
        "--login",
        "--account",
        str(number),
        "--profile",
        str(account_profile_dir(account, number)),
    ]
    if qr:
        extra.append("--qr")
    label = f"вход в VK: {login}"
    wait = gap - (time.time() - _last_login["t"])
    if wait > 0:
        return False, f"подождите {int(wait)} с между входами — так безопаснее для аккаунтов"
    ok, message = runner.start("vk_auth", extra, label)
    if ok:
        _last_login["t"] = time.time()
    return ok, message or f"{label} — введите код в открывшемся окне браузера"


def action_login(runner: Runner, body: dict) -> tuple[bool, str]:
    """Вход в VK для аккаунта из accounts.txt: браузер открывается, код вводит человек.

    Номер аккаунта — как в vk_auth.py --account N: строка файла, с единицы.
    """
    if _login_batch["active"]:
        return False, "идёт очередь входов — дождитесь её конца или нажмите «Стоп»"
    try:
        number = int(body.get("account") or 0)
    except (TypeError, ValueError):
        number = 0
    return _start_login(runner, number, qr=bool(body.get("qr")))


def action_login_all(runner: Runner, body: dict) -> tuple[bool, str]:
    """Вход во все аккаунты, кому нужен вход, одной кнопкой: окна идут по очереди.

    Очередь берёт аккаунты из таблицы, у которых нет файла сессии или сессия мертва
    (VK ответил «access_token has expired»), и открывает вход по одному: пока человек
    вводит пароль в текущем окне, следующее не открывается.
    body: {"stop": true} — закончить после текущего аккаунта (окно не закрываем);
    {"gap": секунды} — пауза между окнами (по умолчанию LOGIN_BATCH_GAP_SEC).
    """
    if body.get("stop"):
        if not _login_batch["active"]:
            return False, "очередь входа не запущена"
        _login_batch["cancel"] = True
        return True, "очередь входа остановится после текущего аккаунта"
    if _login_batch["active"]:
        return False, "очередь входа уже идёт — дождитесь конца или нажмите «Стоп»"
    if runner.running:
        return False, "сейчас идёт другой процесс — дождитесь окончания или нажмите «Стоп»"
    queue = [
        {"number": int(row["number"]), "login": str(row["login"])}
        for row in sorted(accounts_view(), key=lambda item: int(item["number"]))
        if row["needs_login"]
    ]
    if not queue:
        return False, "нет аккаунтов, кому нужен вход — у всех рабочая сессия"
    try:
        gap = float(body["gap"])
    except (KeyError, TypeError, ValueError):
        gap = float(LOGIN_BATCH_GAP_SEC)
    gap = min(max(gap, 0.0), float(LOGIN_GAP_SEC))
    _login_batch.update(
        active=True, cancel=False, done=0, failed=0, total=len(queue), login=""
    )
    threading.Thread(target=_login_batch_worker, args=(runner, queue, gap), daemon=True).start()
    return True, (
        f"вход в VK для {len(queue)} аккаунтов без сессии или с протухшим токеном: "
        f"окна откроются по очереди (пауза {int(gap)} с) — пароль и капчу вводите "
        f"в каждом окне. Остановить: «Стоп»"
    )


def _login_batch_worker(runner: Runner, queue: list[dict], gap: float) -> None:
    """Открывает окна входа по одному: дождался человека в текущем — берёт следующий."""
    total = len(queue)
    runner.log(f"очередь входа: аккаунтов, кому нужен вход, {total}, пауза {int(gap)} с")
    for index, item in enumerate(queue, 1):
        if not _wait_login_gap(gap):
            break
        login = str(item["login"])
        _login_batch["login"] = login
        runner.log(f"очередь входа: {index}/{total} — {login}")
        ok, message = _start_login(runner, int(item["number"]), gap=gap)
        if not ok:
            _login_batch["failed"] += 1
            runner.log(f"очередь входа: {login} пропущен — {message}")
            continue
        deadline = time.time() + LOGIN_BATCH_TIMEOUT_SEC
        while runner.running and not _login_batch["cancel"] and time.time() < deadline:
            time.sleep(0.5)
        if runner.running:
            runner.log(f"очередь входа: {login} не завершился за {LOGIN_BATCH_TIMEOUT_SEC} с — иду дальше")
        _login_batch["done"] += 1
        _login_batch["login"] = ""
    _login_batch["active"] = False
    _login_batch["login"] = ""
    runner.log(
        f"очередь входа: готово — входов {_login_batch['done']} из {total}, "
        f"пропущено {_login_batch['failed']}"
    )


def autologin_after_failure(runner: Runner, label: str, code: int) -> None:
    """Прогон не запустился из-за сессий («код 2») — сам открывает очередь входа.

    Панель раньше только писала «ПРЕДПОЛЁТ: готовых аккаунтов нет — прогон не
    запущен», и работа стояла до тех пор, пока человек не нажмёт «Войти во все,
    кому нужен вход». Теперь очередь входа стартует сама: окна идут по одному,
    пароль и капчу (если VK их спросит) вводит человек — а часто вход проходит и
    без него, профиль браузера ещё помнит аккаунт. Выключить: настройка
    auto_login_on_fail (data/settings.json) — тогда вход только кнопкой.
    """
    if label != RUN_LABEL or int(code or 0) != 2:
        return
    if _login_batch["active"] or runner.running:
        return
    if not bool(load_settings().get("auto_login_on_fail", True)):
        runner.log(
            "автовход: выключен (auto_login_on_fail) — войдите кнопкой "
            "«Войти во все, кому нужен вход» на странице «Аккаунты»"
        )
        return
    ok, message = action_login_all(runner, {})
    if ok:
        runner.log(f"автовход: прогон не запустился (код 2, готовых аккаунтов нет) — {message}")
    else:
        runner.log(f"автовход: очередь входа не запущена — {message}")


def action_stop(runner: Runner) -> tuple[bool, str]:
    """«Стоп»: гасит процесс, а если идёт очередь входа — закрывает и её."""
    if _login_batch["active"]:
        _login_batch["cancel"] = True
        if not runner.running:
            return True, "очередь входа остановится после текущего аккаунта"
    return runner.stop()


def action_save_settings(body: dict) -> tuple[bool, str]:
    settings = load_settings()
    if "max_accounts" in body:
        settings["max_accounts"] = max(1, int(body["max_accounts"]))
    if "repeat_passes" in body:
        settings["repeat_passes"] = max(1, int(body["repeat_passes"]))
    if "default_days" in body:
        settings["default_days"] = max(0.01, float(body["default_days"]))
    if "request_pause_sec" in body:
        settings["request_pause_sec"] = max(0.0, float(body["request_pause_sec"]))
    if "auto_login_on_fail" in body:
        settings["auto_login_on_fail"] = bool(body["auto_login_on_fail"])
    if "show_browser" in body:
        # Показ окна браузера-эмулятора: видно, как он обновляет ленту через F5.
        settings["show_browser"] = bool(body["show_browser"])
    if "tag_query" in body:
        # Чем искать тег в запросе к VK: «с #» (хештег) или «без #» (слово) — у VK это
        # разные выдачи. Значение идёт и подписью («без # (слово)»), и режимом.
        settings["tag_query"] = tag_query_value(str(body.get("tag_query") or ""))
    try:
        save_settings(settings)
    except OSError as exc:
        return False, f"настройки не сохранились: {exc}"
    if "tag_query" in body:
        return True, (
            "настройки сохранены: тег ищу "
            f"{TAG_QUERY_LABELS[settings['tag_query']]} — это же применится к прогону"
        )
    return True, f"настройки сохранены: потоков {settings['max_accounts']}, проходов {settings['repeat_passes']}"


# HTTP: статика + JSON API
class Handler(BaseHTTPRequestHandler):
    """Веб-панель: раздаёт три файла из src/webui и отвечает по /api/…."""

    server_version = "vk-feed-panel"
    protocol_version = "HTTP/1.1"
    runner: Runner  # подставляется при сборке сервера

    def log_message(self, fmt: str, *args) -> None:
        """Тишина: вывод панели идёт в свой лог, а не в консоль."""
        pass

    # ответы
    def _send(self, payload: dict, code: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, target: Path, ctype: str) -> None:
        if not target.exists():
            self._send({"ok": False, "message": f"нет файла {target.name}"}, 404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _result(self, ok: bool, message: str) -> None:
        self._send({"ok": bool(ok), "message": message})

    def _editable(self, path: str) -> Optional[Path]:
        return EDITABLE.get(path.rsplit("/", 1)[-1])

    # GET
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/style.css":
            return self._send_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self._send_file(WEB_DIR / "app.js", "text/javascript; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if path == "/api/status":
                return self._send(status_payload(self.runner, int(self.server.server_address[1])))
            if path == "/api/progress":
                return self._send(progress_payload(self.runner))
            if path == "/api/logs":
                after = int((query.get("after") or ["0"])[0] or 0)
                lines, seq = self.runner.tail(after)
                return self._send(
                    {
                        "lines": lines,
                        "next": seq,
                        "running": self.runner.running,
                        "state": self.runner.state_text(),
                    }
                )
            if path == "/api/accounts":
                return self._send({"rows": accounts_view()})
            if path == "/api/tags":
                return self._send({"rows": tags_view()})
            if path == "/api/events":
                limit = int((query.get("limit") or ["40"])[0] or 40)
                return self._send({"rows": events_view(limit)})
            if path == "/api/settings":
                return self._send(load_settings())
            if path.startswith("/api/files/"):
                target = self._editable(path)
                if target is None:
                    return self._send({"ok": False, "message": "неизвестный файл"}, 404)
                kind = path.rsplit("/", 1)[-1]
                return self._send(
                    {"ok": True, "kind": kind, "path": str(target), "content": read_file(target)}
                )
        except Exception as exc:  # noqa: BLE001
            return self._send({"ok": False, "message": f"ошибка сервера: {exc}"}, 500)
        self._send({"ok": False, "message": f"нет маршрута {path}"}, 404)

    # POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/run":
                return self._result(*action_run(self.runner, body))
            if path == "/api/stop":
                return self._result(*action_stop(self.runner))
            if path == "/api/export":
                return self._result(*self._script("export_xlsx", [], "выгрузка в Excel"))
            if path == "/api/validate":
                workers = max(1, int(body.get("workers") or load_settings()["max_accounts"]))
                return self._result(
                    *self._script(
                        "account_pool",
                        ["--validate", "--workers", str(workers)],
                        "проверка аккаунтов",
                    )
                )
            if path == "/api/proxies":
                return self._result(*self._script("check_proxy", [], "проверка прокси"))
            if path == "/api/tags/add":
                return self._result(*action_add_tags(body))
            if path == "/api/tags/reset":
                return self._result(*action_reset_tag(body))
            if path == "/api/tags/reset-stats":
                return self._result(*action_reset_tag_stats(body))
            if path == "/api/accounts/add":
                return self._result(*action_add_accounts(body))
            if path == "/api/proxies/add":
                return self._result(*action_add_proxies(body))
            if path == "/api/proxies/bind":
                return self._result(*action_bind_proxies())
            if path == "/api/login":
                return self._result(*action_login(self.runner, body))
            if path == "/api/login/all":
                return self._result(*action_login_all(self.runner, body))
            if path == "/api/accounts/delete":
                return self._result(*action_delete_accounts(self.runner, body))
            if path == "/api/open":
                target = str(body.get("target") or "")
                targets = {
                    "data": DATA_DIR,
                    "exports": EXPORTS_DIR,
                    "logs": DATA_DIR / "logs",
                    **EDITABLE,
                }
                chosen = targets.get(target)
                if chosen is None:
                    return self._result(False, f"нечего открывать: {target}")
                if open_path(chosen):
                    return self._result(True, f"открываю {chosen}")
                # На сервере без графики открывать папки нечем — показываем путь
                return self._result(True, f"файловый менеджер недоступен — путь: {chosen}")
            if path == "/api/settings":
                # Настройки страница сохраняет методом PUT (основной маршрут, см.
                # do_PUT). POST принимаем тоже: у уже открытой страницы в браузере
                # может лежать старый app.js, а «нет маршрута /api/settings» человеку
                # ничего не объясняет — выбор «с #» / «без #» просто не сохранялся.
                return self._result(*action_save_settings(body))
        except Exception as exc:  # noqa: BLE001
            return self._send({"ok": False, "message": f"ошибка: {exc}"}, 500)
        self._send({"ok": False, "message": f"нет маршрута {path}"}, 404)

    def _script(self, script: str, extra: list, label: str) -> tuple[bool, str]:
        ok, message = self.runner.start(script, list(extra), label)
        return ok, message or f"{label}: запущено"

    # PUT
    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/settings":
                return self._result(*action_save_settings(body))
            if path.startswith("/api/files/"):
                target = self._editable(path)
                if target is None:
                    return self._send({"ok": False, "message": "неизвестный файл"}, 404)
                write_file(target, str(body.get("content") or ""))
                return self._result(True, f"{target.name} сохранён")
        except Exception as exc:  # noqa: BLE001
            return self._send({"ok": False, "message": f"ошибка: {exc}"}, 500)
        self._send({"ok": False, "message": f"нет маршрута {path}"}, 404)


# Запуск панели
def arg_value(args: list[str], name: str) -> str:
    """Значение ключа вида `--port 9000` (пустая строка, если ключа нет)."""
    if name not in args:
        return ""
    index = args.index(name)
    if index + 1 >= len(args):
        return ""
    return args[index + 1].strip()


def is_local_host(host: str) -> bool:
    """True — панель слушает только этот компьютер (безопасный вариант)."""
    return (host or "").strip().lower() in {"", "127.0.0.1", "localhost", "::1"}


def _open_browser(url: str) -> None:
    """Открывает браузер и молчит, если графики нет (сервер, ssh)."""
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def pick_port(preferred: int, tries: int = 15, host: str = HOST) -> int:
    """Порт занят — пробуем следующий (не падаем с ошибкой из-за мелочи)."""
    last_error: Optional[OSError] = None
    for candidate in range(preferred, preferred + tries):
        with socket.socket() as probe:
            try:
                probe.bind((host, candidate))
            except socket.gaierror as exc:
                raise SystemExit(f"хост «{host}» не найден: {exc}") from exc
            except OSError as exc:
                last_error = exc
                continue
            return candidate
    raise SystemExit(
        f"нет свободного порта в диапазоне {preferred}-{preferred + tries} "
        f"для {host} ({last_error})"
    )


def build_server(port: int, host: str = HOST) -> tuple[ThreadingHTTPServer, Runner]:
    runner = Runner()
    handler = type("PanelHandler", (Handler,), {"runner": runner})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, runner


def selftest(port: int = DEFAULT_PORT + 101) -> int:
    """Проверяет страницу, API и запуск процесса — без браузера."""
    from urllib import request as urlrequest

    port = pick_port(port)
    httpd, runner = build_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://{HOST}:{port}"
    problems: list[str] = []
    checked = 0

    def fetch(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, str]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urlrequest.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlrequest.urlopen(request, timeout=5) as response:
            return int(response.status), response.read().decode("utf-8")

    def expect(path: str, checker, method: str = "GET", payload: dict | None = None) -> None:
        nonlocal checked
        try:
            status, raw = fetch(path, method, payload)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            checker(json.loads(raw))
            checked += 1
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path}: {exc}")

    try:
        for static in ("/", "/style.css", "/app.js"):
            try:
                status, raw = fetch(static)
                if status != 200 or not raw.strip():
                    problems.append(f"{static}: HTTP {status}, пусто")
                else:
                    checked += 1
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{static}: {exc}")

        expect("/api/status", lambda data: bool(data["files"]["tags"]))
        expect(
            "/api/progress",
            lambda data: int(data["posts_total"]) >= 0 and "login_batch" in data
            and int(data["posts_since_reset"]) >= 0  # карточка «Постов в базе»
            and int(data["tag_links_since_reset"]) >= 0
            and float(data["counter_reset_at"]) >= 0,
        )
        expect("/api/accounts", lambda data: isinstance(data["rows"], list))
        expect("/api/tags", lambda data: isinstance(data["rows"], list))
        expect("/api/events", lambda data: isinstance(data["rows"], list))
        expect("/api/settings", lambda data: "max_accounts" in data)
        expect("/api/files/tags", lambda data: isinstance(data["content"], str))
        expect("/api/logs?after=0", lambda data: isinstance(data["lines"], list))

        # Сохранение настроек: страница шлёт выбор «с #» / «без #» методом PUT, а POST
        # принимается для уже открытой страницы со старым app.js. Проверяем на временном
        # файле настроек — настоящий data/settings.json самопроверка трогать не должна.
        # Маршрут потеряешь — панель ответит «нет маршрута /api/settings», и режим тега
        # молча не сохранится.
        import settings as settings_mod
        import tempfile

        real_config = settings_mod.CONFIG_FILE
        tmp_dir = tempfile.TemporaryDirectory(prefix="panel_settings_")
        settings_mod.CONFIG_FILE = Path(tmp_dir.name) / "settings.json"
        try:
            for method in ("PUT", "POST"):
                expect(
                    "/api/settings",
                    lambda data: bool(data["ok"]) and "настройки сохранены" in data["message"],
                    method,
                    {"tag_query": "word"},
                )
            saved = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("tag_query") == "word":
                checked += 1
            else:
                problems.append("/api/settings: режим тега не попал в файл настроек")
        finally:
            settings_mod.CONFIG_FILE = real_config
            tmp_dir.cleanup()

        ok, message = runner.spawn(
            [sys.executable, "-c", "print('проверка лога панели')"], "тест лога"
        )
        if not ok:
            problems.append(f"процесс не запустился: {message}")
        else:
            deadline = time.time() + 10
            while runner.running and time.time() < deadline:
                time.sleep(0.1)
            lines, _ = runner.tail(0)
            if any("проверка лога панели" in line for line in lines):
                checked += 1
            else:
                problems.append("вывод процесса не попал в лог панели")
    finally:
        httpd.shutdown()
        httpd.server_close()

    if problems:
        print("проверка панели: есть проблемы")
        for item in problems:
            print(f"  • {item}")
        return 1
    print(f"проверка панели: ок ({checked} проверок, порт {port})")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Поднимает панель, открывает браузер и живёт до Ctrl+C."""
    args = list(sys.argv[1:] if argv is None else argv)
    ensure_layout()
    preferred = DEFAULT_PORT
    raw_port = arg_value(args, "--port")
    if raw_port:
        try:
            preferred = int(raw_port)
        except ValueError:
            print(f"порт «{raw_port}» не число — беру {DEFAULT_PORT}")
    host = arg_value(args, "--host") or HOST

    if "--selftest" in args:
        return selftest(preferred)

    port = pick_port(preferred, host=host)
    httpd, runner = build_server(port, host=host)
    local_url = f"http://127.0.0.1:{port}/"
    url = local_url if is_local_host(host) else f"http://{host}:{port}/"
    runner.log(f"панель поднята: {url}")
    runner.log(f"данные: {DATA_DIR}")
    print("=" * 62)
    if is_local_host(host):
        print(f"  Панель открыта в браузере: {url}")
    else:
        print(f"  Панель доступна по сети: {url}")
        print(f"  На этом компьютере: {local_url}")
        print("  ВНИМАНИЕ: у панели нет пароля — не публикуйте её в интернет.")
        print(f"  Безопаснее SSH-туннель: ssh -L {port}:127.0.0.1:{port} user@сервер")
    print("  Это окно — сервер панели: пока оно открыто, панель работает.")
    print("  Закрыть панель: Ctrl+C здесь или просто закрыть это окно.")
    print("=" * 62, flush=True)

    if "--no-browser" not in args:
        if can_open_browser():
            threading.Timer(0.8, lambda: _open_browser(local_url)).start()
        else:
            runner.log(
                f"графической сессии нет ({os_label()}) — браузер не открываю. "
                f"Откройте вручную: {local_url} (или пробросьте порт по SSH)"
            )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаю панель…")
    finally:
        if runner.running:
            runner.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
