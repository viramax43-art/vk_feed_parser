#!/usr/bin/env python3
"""Точка входа для exe: панель открывается отдельным окном программы.

Окно (по умолчанию — Edge/Chrome в режиме приложения):
  * не нужен pywebview, pythonnet и .NET, поэтому ошибка
    "Failed to resolve Python.Runtime.Loader.initialize" невозможна;
  * окно без вкладок и адресной строки — выглядит как обычная программа;
  * `--window=webview` пробует нативное окно pywebview, при сбое возвращается к Edge.

Первая строка data/logs/startup.log содержит метку сборки.

Режимы (один и тот же exe):
  VK Feed Parser.exe                 — окно панели
  VK Feed Parser.exe --selfcheck     — проверить сборку без окна (для build_exe.py)
  VK Feed Parser.exe --run_tags …    — запуск модуля из src/ (его вызывает панель)
"""
from __future__ import annotations

import io
import multiprocessing
import os
import runpy
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

BUILD = "edge-window-2"

FROZEN = bool(getattr(sys, "frozen", False))
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
LOG_DIR = BASE / "data" / "logs"
STARTUP_LOG = LOG_DIR / "startup.log"

if FROZEN:
    SRC = Path(getattr(sys, "_MEIPASS", BASE)) / "src"
else:
    SRC = BASE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RUNNABLE = {
    "run_tags", "vk_auth", "account_pool", "check_proxy", "export_xlsx",
    "probe", "settings", "gui",
}


def guard_streams() -> None:
    """В сборке без консоли stdout/stderr отсутствуют: любой print падает.

    Подменяем их заглушкой, чтобы сообщения не роняли программу, а шли в журнал.
    """
    if os.name != "nt" or not FROZEN:
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    sink = io.StringIO()

    class ToLog(io.StringIO):
        def write(self, text):  # type: ignore[override]
            if text and text.strip():
                log(f"stdout: {text.rstrip()}")
            return len(text)

        def flush(self) -> None:
            return None

    sys.stdout = ToLog()
    sys.stderr = ToLog()
    del sink


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with STARTUP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def alert(title: str, text: str) -> None:
    log(f"ALERT {title}: {text}")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(f"{title}\n{text}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def run_module(name: str, argv: list[str]) -> int:
    sys.argv = [f"{name}.py", *argv]
    try:
        runpy.run_path(str(SRC / f"{name}.py"), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    return 0


def dispatch(argv: list[str]):
    if not argv or not argv[0].startswith("--"):
        return None
    name = argv[0][2:]
    if name not in RUNNABLE:
        return None
    return run_module(name, argv[1:])


def app_mode_browser() -> str | None:
    """Путь к Edge или Chrome. В Windows Edge есть почти всегда."""
    candidates: list[Path] = []
    if os.name == "nt":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates += [
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                ]
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def open_app_window(url: str) -> bool:
    """Окно Edge/Chrome в режиме приложения. Ждёт закрытия. False — браузера нет."""
    browser = app_mode_browser()
    if not browser:
        log("app-window: Edge/Chrome не найден")
        return False
    profile = BASE / "data" / "app_window_profile"
    profile.mkdir(parents=True, exist_ok=True)
    log(f"app-window: {browser}")
    try:
        proc = subprocess.Popen([
            browser, f"--app={url}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--window-size=1280,860",
        ])
    except OSError as exc:
        log(f"app-window: не запустился ({exc})")
        return False
    proc.wait()
    return True


def open_webview_window(url: str) -> None:
    """Нативное окно pywebview. Нет pywebview или сбой загрузки — исключение."""
    import webview

    log("pywebview imported")
    webview.create_window("VK Feed Parser", url, width=1280, height=860, min_size=(900, 600))
    webview.start()
    log("pywebview window closed")


def start_server():
    import webui

    log(f"webui imported, data dir: {webui.DATA_DIR}")
    port = webui.pick_port(webui.DEFAULT_PORT)
    httpd, runner = webui.build_server(port)
    url = f"http://{webui.HOST}:{port}/"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"server up: {url}")
    return webui, httpd, runner, url


def cleanup(runner, httpd) -> None:
    try:
        if runner.running:
            runner.stop()
        httpd.shutdown()
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass


def selfcheck() -> int:
    """Проверка сборки без окна: панель импортируется и отвечает по HTTP.

    Нужна сборщику: если что-то не попало в exe, это видно сразу, а не у клиента.
    """
    log(f"BUILD {BUILD} | selfcheck start")
    try:
        from urllib import request as urlrequest

        webui, httpd, runner, url = start_server()
    except BaseException as exc:  # noqa: BLE001
        log("SELFCHECK FAILED:\n" + traceback.format_exc())
        alert("VK Feed Parser — сборка нерабочая",
              f"Панель не импортируется: {exc}\n\nПодробности: {STARTUP_LOG}")
        return 1
    problems: list[str] = []
    try:
        for path in ("/", "/app.js", "/api/status", "/api/accounts"):
            try:
                with urlrequest.urlopen(url.rstrip("/") + path, timeout=8) as resp:
                    body = resp.read()
                if resp.status != 200 or not body:
                    problems.append(f"{path}: HTTP {resp.status}, пусто")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{path}: {exc}")
    finally:
        cleanup(runner, httpd)
    if problems:
        log("SELFCHECK problems: " + "; ".join(problems))
        alert("VK Feed Parser — сборка нерабочая", "\n".join(problems) + f"\n\nПодробности: {STARTUP_LOG}")
        return 1
    log("SELFCHECK OK")
    return 0


def window_mode(argv: list[str]) -> str:
    for arg in argv:
        if arg.startswith("--window="):
            return arg.split("=", 1)[1].strip().lower()
    return "edge"


def main() -> int:
    routed = dispatch(sys.argv[1:])
    if routed is not None:
        return routed
    if "--selfcheck" in sys.argv[1:]:
        return selfcheck()

    log(f"BUILD {BUILD} | start frozen={FROZEN} python={sys.version.split()[0]} base={BASE}")
    try:
        webui, httpd, runner, url = start_server()
    except BaseException as exc:  # noqa: BLE001
        log("STARTUP FAILED:\n" + traceback.format_exc())
        alert("VK Feed Parser — не запустилась панель",
              f"Причина: {exc}\n\nПодробности: {STARTUP_LOG}")
        return 1

    opened = False
    try:
        if window_mode(sys.argv[1:]) == "webview":
            try:
                open_webview_window(url)
                opened = True
            except BaseException:  # noqa: BLE001
                log("pywebview FAILED, перехожу на окно Edge:\n" + traceback.format_exc())
        if not opened:
            opened = open_app_window(url)
    except KeyboardInterrupt:
        cleanup(runner, httpd)
        return 0

    if opened:
        log("окно закрыто пользователем")
        cleanup(runner, httpd)
        return 0

    alert(
        "VK Feed Parser — окно не открылось",
        "Не найден Microsoft Edge или Chrome, поэтому окно открыть не удалось.\n\n"
        "Панель уже работает — откройте её в любом браузере по адресу:\n"
        f"{url}\n\n"
        f"Подробности: {STARTUP_LOG}",
    )
    log(f"окна нет; сервер оставлен работать: {url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 1
    finally:
        cleanup(runner, httpd)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # нужно PyInstaller на Windows
    guard_streams()
    log(f"BUILD {BUILD} | invoke argv={sys.argv[1:]}")
    code = 1
    try:
        code = main()
    except BaseException as exc:  # noqa: BLE001
        log("UNHANDLED:\n" + traceback.format_exc())
        alert("VK Feed Parser — ошибка", f"{exc}\n\nПодробности: {STARTUP_LOG}")
    raise SystemExit(code)
