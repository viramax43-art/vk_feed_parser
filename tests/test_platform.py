#!/usr/bin/env python3
"""Проверка кроссплатформенности: Windows, macOS и Linux.

    python tests/test_platform.py

Сеть, браузер и playwright не нужны: признаки ОС (IS_WINDOWS/IS_LINUX/…) и функции
platform_compat подменяются, и мы проверяем, что код выбирает правильный браузер,
правильные аргументы Chromium, адрес системного прокси и умеет поднимать панель на
сервере (--host), а на сервере без графики честно об этом говорит.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # start_panel.py лежит в корне проекта

import check_proxy  # noqa: E402
import gui  # noqa: E402
import platform_compat as pc  # noqa: E402
import start_panel  # noqa: E402
import vk_auth  # noqa: E402
import webui  # noqa: E402
from accounts import Account  # noqa: E402
from paths import invoke_cmd  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


@contextlib.contextmanager
def fake_os(windows: bool = False, macos: bool = False, linux: bool = False, root: bool = False,
            display: bool = True):
    """Подменяет признаки ОС, как будто код запущен в другой системе."""
    saved = {
        "IS_WINDOWS": pc.IS_WINDOWS,
        "IS_MACOS": pc.IS_MACOS,
        "IS_LINUX": pc.IS_LINUX,
        "IS_POSIX": pc.IS_POSIX,
        "is_root": pc.is_root,
        "has_display": pc.has_display,
    }
    pc.IS_WINDOWS, pc.IS_MACOS, pc.IS_LINUX = windows, macos, linux
    pc.IS_POSIX = macos or linux
    pc.is_root = lambda: root
    pc.has_display = lambda: display
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(pc, key, value)


# 1. Признаки системы
check(
    sum([pc.IS_WINDOWS, pc.IS_MACOS, pc.IS_LINUX]) <= 1,
    f"система определяется однозначно: {pc.os_label()}",
)
check(bool(pc.os_label()), f"подпись системы не пустая: {pc.os_label()}")
if pc.IS_WINDOWS:
    check(pc.has_display() and not pc.is_root(), "Windows: графика есть, root не бывает")
if os.name != "posix":
    check(not pc.is_root(), "не POSIX: root не определяется")

# 2. Каналы браузера: порядок попыток зависит от системы
with fake_os(windows=True):
    channels = pc.browser_channels()
    check(
        channels[0] == "msedge" and "chrome" in channels and channels[-1] is None,
        f"Windows: сначала Edge, потом Chrome, в конце встроенный Chromium: {channels}",
    )
with fake_os(macos=True):
    channels = pc.browser_channels()
    check(
        channels[0] == "chrome" and "msedge" in channels and channels[-1] is None,
        f"macOS: сначала Chrome, потом Edge, в конце встроенный Chromium: {channels}",
    )
with fake_os(linux=True):
    channels = pc.browser_channels()
    check(
        channels[0] is None and "chrome" in channels and "chromium" in channels,
        f"Linux: сначала встроенный Chromium, потом системные Chrome/Chromium: {channels}",
    )
    attempts = pc.browser_launch_attempts()
    check(attempts[0] == {}, f"Linux: первая попытка — встроенный Chromium: {attempts[0]}")
    check(
        all(set(item) <= {"channel", "executable_path"} for item in attempts),
        "попытки запуска — только канал или путь к браузеру",
    )
    check(
        any("executable_path" in item for item in attempts) or not pc.system_browser_paths(),
        "если системный браузер найден, есть попытка запуска по пути",
    )
# 3. Порядок попыток запуска браузера: сначала те, что реально стоят в системе
with fake_os(windows=True):
    attempts = pc.browser_launch_attempts()
    installed = pc.installed_browsers()
    channels = [item.get("channel") for item in attempts if item.get("channel")]
    present = [c for c in channels if c in installed]
    check(
        channels[: len(present)] == present,
        f"Windows: сначала каналы установленных браузеров ({sorted(installed) or 'нет'}): {channels}",
    )
    check(
        set(channels) == {"msedge", "chrome"} and {} in attempts,
        f"порядок меняется, набор попыток — нет: {attempts[:4]}",
    )
    check(
        pc.browser_not_installed("BrowserType.launch_persistent_context: Chromium distribution"
                                 " 'msedge' is not found at C:\\Edge\\msedge.exe")
        and not pc.browser_not_installed("BrowserType.launch: Target page, context or browser has been closed"),
        "«браузер не установлен» отличается от «не запустился»",
    )

# 3. Аргументы Chromium: под root и в контейнере нужны свои ключи
with fake_os(linux=True, root=False):
    args = pc.browser_args()
    check(
        "--no-sandbox" not in args and "--disable-dev-shm-usage" in args,
        f"Linux без root: --no-sandbox не нужен, /dev/shm учитывается: {args}",
    )
with fake_os(linux=True, root=True):
    args = pc.browser_args(["--disable-blink-features=AutomationControlled"])
    check(
        "--no-sandbox" in args and "--disable-blink-features=AutomationControlled" in args,
        f"Linux под root: --no-sandbox добавлен, свои ключи сохранены: {args}",
    )
    check(pc.browser_args(["--no-sandbox"]).count("--no-sandbox") == 1, "--no-sandbox не дублируется")
with fake_os(windows=True):
    base = ["--disable-blink-features=AutomationControlled"]
    check(pc.browser_args(base) == base, "Windows: аргументы не меняются")
    check(pc.browser_args([]) == [], "Windows: пустой список остаётся пустым")

# 4. Подсказки: у каждой системы свой текст
with fake_os(linux=True):
    hint = pc.browser_missing_hint("нет браузера")
    check(
        "playwright install" in hint and "нет браузера" in hint,
        "Linux: в ошибке есть команда установки и причина",
    )
    check("xvfb" in pc.headed_login_hint(), "Linux: подсказка про видимый вход упоминает xvfb")
with fake_os(windows=True):
    check("Edge" in pc.browser_missing_hint(), "Windows: в ошибке упоминается Edge")
    check("Windows" in pc.proxy_warning("1.2.3.4:8080"), "Windows: прокси упоминает Windows")
with fake_os(linux=True):
    check(
        "GNOME" in pc.proxy_warning("1.2.3.4:8080"),
        "Linux: прокси упоминает окружение и настройки GNOME",
    )

# 5. Системный прокси: на Linux читается из окружения
with fake_os(linux=True):
    saved_env = {name: os.environ.get(name) for name in pc.PROXY_ENV_NAMES}
    for name in pc.PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    try:
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1080"
        check(pc.system_proxy() == "http://127.0.0.1:1080", "Linux: прокси берётся из HTTPS_PROXY")
        check(check_proxy.system_proxy_state() == "http://127.0.0.1:1080", "check_proxy отдаёт тот же адрес")
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
with fake_os(macos=True):
    saved_env = {name: os.environ.get(name) for name in pc.PROXY_ENV_NAMES}
    for name in pc.PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    try:
        check(pc.system_proxy() == "", "macOS: без переменных окружения прокси не выдумывается")
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

# 6. Права на сессию: на Linux файл только для владельца
with tempfile.TemporaryDirectory(prefix="platform_") as raw_dir:
    secret = Path(raw_dir) / "session.json"
    secret.write_text("{}", encoding="utf-8")
    try:
        pc.private_file(secret)
        check(secret.exists(), "private_file не удаляет файл сессии")
    except Exception as exc:  # noqa: BLE001
        check(False, f"private_file не падает: {exc}")
    if os.name == "posix":
        check(secret.stat().st_mode & 0o777 == 0o600, "POSIX: права на файл сессии 0600")


# 7. Вход в VK: перебор браузеров и понятная ошибка на сервере без графики
class FakeChromium:
    """Заглушка Playwright: первый браузер «не найден», второй запускается."""

    def __init__(self, fail_first: int = 1, fail_all: bool = False):
        self.calls: list[dict] = []
        self.fail_first = fail_first
        self.fail_all = fail_all

    def launch_persistent_context(self, profile, **kwargs):
        self.calls.append(kwargs)
        if self.fail_all or len(self.calls) <= self.fail_first:
            raise RuntimeError("Executable doesn't exist at /home/user/.cache/ms-playwright")
        return type("Context", (), {"pages": [], "ok": True})()


class FakePw:
    def __init__(self, chromium):
        self.chromium = chromium


saved_attempts = vk_auth.browser_launch_attempts
saved_log = vk_auth.log
vk_auth.browser_launch_attempts = lambda: [{"channel": "chrome"}, {}]
vk_auth.log = lambda msg: None
try:
    chromium = FakeChromium(fail_first=1)
    context = vk_auth._launch_context(FakePw(chromium), Path("data/profile"), {"headless": True})
    check(getattr(context, "ok", False), "вход: нерабочий браузер — берём следующий")
    check(
        chromium.calls[-1] == {"headless": True},
        f"вход: вторая попытка без канала (встроенный Chromium): {chromium.calls[-1]}",
    )

    chromium = FakeChromium(fail_all=True)
    try:
        vk_auth._launch_context(FakePw(chromium), Path("data/profile"), {"headless": True})
        check(False, "вход: если браузеров нет — понятная ошибка")
    except vk_auth.VkAuthError as exc:
        check("Edge" in str(exc) or "playwright install" in str(exc), f"вход: текст ошибки понятный: {str(exc)[:70]}…")
        check(len(chromium.calls) == 2, f"вход: перебрали все попытки ({len(chromium.calls)})")
finally:
    vk_auth.browser_launch_attempts = saved_attempts
    vk_auth.log = saved_log

saved_display = vk_auth.has_display
saved_layout = vk_auth.ensure_layout
vk_auth.has_display = lambda: False
vk_auth.ensure_layout = lambda: None
try:
    account = Account(login="+79990000001", password="secret")
    try:
        vk_auth.login_interactive(account, "1.2.3.4:8080:user:pass", headless=False)
        check(False, "видимый вход без графики — ошибка до запуска браузера")
    except vk_auth.VkAuthError as exc:
        check("xvfb" in str(exc), f"видимый вход без графики: подсказка про xvfb: {str(exc)[:60]}…")
    except ImportError as exc:  # playwright не установлен — это другая ветка
        check(False, f"проверка графики срабатывает до импорта playwright: {exc}")
finally:
    vk_auth.has_display = saved_display
    vk_auth.ensure_layout = saved_layout

# 8. Открытие папок: без графики честно возвращаем False
saved_flags = (gui.IS_WINDOWS, gui.IS_MACOS)
saved_which = gui.shutil.which
gui.IS_WINDOWS = False
gui.IS_MACOS = False
gui.shutil.which = lambda name: None
try:
    check(gui.open_path(ROOT) is False, "сервер без xdg-open: open_path честно возвращает False")
finally:
    gui.IS_WINDOWS, gui.IS_MACOS = saved_flags
    gui.shutil.which = saved_which

# 9. Панель на сервере: --host и разбор адреса
check(webui.arg_value(["--port", "9000", "--host", "0.0.0.0"], "--host") == "0.0.0.0", "ключ --host читается")
check(webui.arg_value(["--port", "9000"], "--host") == "", "без --host значение пустое")
check(webui.arg_value(["--host"], "--host") == "", "ключ без значения не ломает запуск")
for host in ("127.0.0.1", "localhost", "::1", ""):
    check(webui.is_local_host(host), f"локальный адрес панели: {host or '—'}")
for host in ("0.0.0.0", "10.0.0.5"):
    check(not webui.is_local_host(host), f"адрес по сети требует предупреждения: {host}")
port = webui.pick_port(webui.DEFAULT_PORT + 777, host="0.0.0.0")
httpd, runner = webui.build_server(port, host="0.0.0.0")
check(httpd.server_address[0] == "0.0.0.0", f"панель слушает указанный адрес: {httpd.server_address}")
httpd.server_close()
check(
    webui.build_server.__defaults__ == (webui.HOST,),
    f"по умолчанию панель слушает только этот компьютер: {webui.HOST}",
)

# 10. Запуск модулей и интерпретатор .venv
cmd = invoke_cmd("run_tags")
check(cmd[0] == sys.executable and cmd[-1].endswith("run_tags.py"), f"команда запуска модуля: {cmd}")
check(Path(cmd[-1]).exists(), "модуль по этой команде существует")
venv_py = start_panel.venv_python()
check(venv_py.name == ("python.exe" if os.name == "nt" else "python"), f"python в .venv: {venv_py.name}")

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "КОД РАБОТАЕТ НА ВСЕХ СИСТЕМАХ")
raise SystemExit(1 if FAILED else 0)
