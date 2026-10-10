#!/usr/bin/env python3
"""Панель массового запуска в браузере: аккаунты, прогон, теги, события, лог.

Запуск:
    python src/webui.py                 # поднять панель и открыть браузер
    python src/webui.py --port 9000     # другой порт
    python src/webui.py --no-browser    # не открывать браузер
    python src/webui.py --selftest      # проверить API и выйти

Сервер слушает только 127.0.0.1 — снаружи панель недоступна. Прогон идёт отдельным
процессом (src/run_tags.py), «Стоп» посылает ему Ctrl+C, состояние панель читает из
data/parser.sqlite3. Сборка не нужна: чистый Python и один HTML-файл рядом.
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

from account_pool import DEAD, HOLD, READY, normalize_status, status_label  # noqa: E402
from accounts import (  # noqa: E402
    load_accounts,
    parse_account_line,
    proxies_from_file,
    resolve_proxy,
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
    pool_summary,
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
from settings import load_settings, save_settings  # noqa: E402
from state_db import DB  # noqa: E402

WEB_DIR = SRC_DIR / "webui"
HOST = "127.0.0.1"
DEFAULT_PORT = 8787
LOG_TAIL = 4000  # сколько строк лога держим в буфере

# Файлы, которые панель показывает и сохраняет.
EDITABLE = {
    "tags": TAGS_FILE,
    "accounts": ACCOUNTS_FILE,
    "proxies": PROXIES_FILE,
}

# Процесс прогона
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

    def _kill_later(self, proc: subprocess.Popen, delay: float = 15.0) -> None:
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


def status_payload(runner: Runner, port: int) -> dict:
    return {
        "running": runner.running,
        "label": runner.label,
        "state": runner.state_text(),
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "exports_dir": str(EXPORTS_DIR),
        "files": {kind: str(path) for kind, path in EDITABLE.items()},
        "periods": [{"label": label, "hours": hours} for label, hours in PERIODS],
        "events": EVENT_LEVELS,
        "port": port,
    }


def progress_payload(runner: Runner) -> dict:
    db = DB()
    stats = db.stats()
    rows = db.list_accounts()
    summary = pool_summary(rows)
    accounts_file = load_accounts()
    proxies = proxies_from_file(resolve_proxies_file())
    total, finished = int(stats["tags_total"]), int(stats["tags_finished"])
    return {
        "running": runner.running,
        "state": runner.state_text(),
        "posts_total": int(stats["posts"]),
        "posts_last_hour": int(stats["posts_last_hour"]),
        "tag_links": int(stats["tag_links"]),
        "tags_total": total,
        "tags_finished": finished,
        "tags_pct": round(100.0 * finished / total, 1) if total else 0.0,
        "accounts_total": len(rows),
        "accounts_ready": summary[READY],
        "accounts_hold": summary[HOLD],
        "accounts_dead": summary[DEAD],
        "accounts_file": len(accounts_file),
        "proxies_total": len(proxies),
    }


def accounts_view() -> list[dict]:
    """Таблица аккаунтов: статус, прокси, посты, отлёжка, последняя ошибка."""
    now = time.time()
    accounts = load_accounts(ACCOUNTS_FILE)
    by_login = {acc.name: acc for acc in accounts}
    numbers = {acc.name: number for number, acc in enumerate(accounts, 1)}
    out = []
    for row in DB().list_accounts():
        login = str(row.get("login") or "")
        status = normalize_status(
            str(row.get("status") or READY), float(row.get("cooldown_until") or 0), now
        )
        acc = by_login.get(login)
        # Показываем прокси из конфига (accounts.txt / proxies.txt): именно он пойдёт
        # в работу. В базе лежит значение с прошлой проверки, оно может быть старым,
        # поэтому пустая строка (= direct) остаётся пустой, а не подменяется базой.
        live_proxy = resolve_proxy(acc) if acc else ""
        out.append(
            {
                "login": login,
                "number": numbers.get(login, 0),  # номер строки в accounts.txt (для входа в VK)
                "status": status,
                "status_label": status_label(status),
                "proxy": live_proxy,
                "posts": int(row.get("total_items") or 0),
                "fails": int(row.get("fail_count") or 0),
                "cooldown": cooldown_text(row, now),
                "error": (row.get("last_error") or "").strip(),
                "session": bool(acc and acc.session_path.exists()),
            }
        )
    return sorted(out, key=lambda item: (item["status"] != READY, item["login"]))


def tags_view() -> list[dict]:
    db = DB()
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
    for event in DB().recent_events(limit):
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
    tags_file = Path(str(body.get("tags_file") or TAGS_FILE)).expanduser()

    settings["max_accounts"] = workers
    settings["repeat_passes"] = passes
    try:
        save_settings(settings)
    except OSError as exc:
        runner.log(f"настройки не сохранились: {exc}")

    args = build_run_args(
        hours=hours,
        passes=passes,
        workers=workers,
        validate_first=bool(body.get("validate_first", True)),
        full=bool(body.get("full", False)),
        dry_run=bool(body.get("dry_run", False)),
        tags_file=tags_file,
    )
    ok, message = runner.start("run_tags", args, "прогон")
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
        count = DB().reset_tags([tag])
    except Exception as exc:  # noqa: BLE001
        return False, f"не получилось сбросить «{tag}»: {exc}"
    return True, f"тег «{tag}» вернулся в очередь (сброшено записей: {count})"


def action_add_accounts(body: dict) -> tuple[bool, str]:
    lines = [line.strip() for line in str(body.get("content") or "").splitlines()]
    keep = [
        line
        for line in lines
        if line and not line.startswith("#") and not line.startswith("//")
    ]
    parsed = [line for line in keep if parse_account_line(line, 0)]
    if not parsed:
        return False, "ни одной строки вида телефон;пароль;2FA;прокси не разобрал"
    try:
        with ACCOUNTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(parsed) + "\n")
    except OSError as exc:
        return False, f"не удалось записать {ACCOUNTS_FILE.name}: {exc}"
    return True, (
        f"{ACCOUNTS_FILE.name}: дописано строк {len(parsed)}, "
        f"всего аккаунтов {len(load_accounts(ACCOUNTS_FILE))}"
    )


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


def action_login(runner: Runner, body: dict) -> tuple[bool, str]:
    """Вход в VK для аккаунта из accounts.txt: браузер открывается, код вводит человек.

    Номер аккаунта — как в vk_auth.py --account N: строка файла, с единицы.
    """
    accounts = load_accounts(ACCOUNTS_FILE)
    if not accounts:
        return False, "нет аккаунтов: добавьте строки в accounts.txt"
    try:
        number = int(body.get("account") or 0)
    except (TypeError, ValueError):
        number = 0
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
    if body.get("qr"):
        extra.append("--qr")
    label = f"вход в VK: {login}"
    ok, message = runner.start("vk_auth", extra, label)
    return ok, message or f"{label} — введите код в открывшемся окне браузера"


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
    try:
        save_settings(settings)
    except OSError as exc:
        return False, f"настройки не сохранились: {exc}"
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
                return self._result(*self.runner.stop())
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
            if path == "/api/accounts/add":
                return self._result(*action_add_accounts(body))
            if path == "/api/login":
                return self._result(*action_login(self.runner, body))
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
                open_path(chosen)
                return self._result(True, f"открываю {chosen}")
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
def pick_port(preferred: int, tries: int = 15) -> int:
    """Порт занят — пробуем следующий (не падаем с ошибкой из-за мелочи)."""
    for candidate in range(preferred, preferred + tries):
        with socket.socket() as probe:
            try:
                probe.bind((HOST, candidate))
            except OSError:
                continue
            return candidate
    raise SystemExit(f"нет свободного порта в диапазоне {preferred}-{preferred + tries}")


def build_server(port: int) -> tuple[ThreadingHTTPServer, Runner]:
    runner = Runner()
    handler = type("PanelHandler", (Handler,), {"runner": runner})
    httpd = ThreadingHTTPServer((HOST, port), handler)
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

    def fetch(path: str) -> tuple[int, str]:
        with urlrequest.urlopen(base + path, timeout=10) as response:
            return int(response.status), response.read().decode("utf-8")

    def expect(path: str, checker) -> None:
        nonlocal checked
        try:
            status, raw = fetch(path)
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
        expect("/api/progress", lambda data: int(data["posts_total"]) >= 0)
        expect("/api/accounts", lambda data: isinstance(data["rows"], list))
        expect("/api/tags", lambda data: isinstance(data["rows"], list))
        expect("/api/events", lambda data: isinstance(data["rows"], list))
        expect("/api/settings", lambda data: "max_accounts" in data)
        expect("/api/files/tags", lambda data: isinstance(data["content"], str))
        expect("/api/logs?after=0", lambda data: isinstance(data["lines"], list))

        ok, message = runner.spawn(
            [sys.executable, "-c", "print('проверка лога панели')"], "тест лога"
        )
        if not ok:
            problems.append(f"процесс не запустился: {message}")
        else:
            deadline = time.time() + 20
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
    if "--port" in args:
        index = args.index("--port")
        if index + 1 < len(args):
            try:
                preferred = int(args[index + 1])
            except ValueError:
                print(f"порт «{args[index + 1]}» не число — беру {DEFAULT_PORT}")

    if "--selftest" in args:
        return selftest(preferred)

    port = pick_port(preferred)
    httpd, runner = build_server(port)
    url = f"http://{HOST}:{port}/"
    runner.log(f"панель поднята: {url}")
    runner.log(f"данные: {DATA_DIR}")
    print("=" * 62)
    print(f"  Панель открыта в браузере: {url}")
    print("  Это окно — сервер панели: пока оно открыто, панель работает.")
    print("  Закрыть панель: Ctrl+C здесь или просто закрыть это окно.")
    print("=" * 62, flush=True)

    if "--no-browser" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
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
