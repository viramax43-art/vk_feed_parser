#!/usr/bin/env python3
"""Различия операционных систем в одном месте: Windows, macOS и Linux.

Проект одинаково работает на Windows (тестирование) и на Linux-сервере
(боевой запуск), поэтому всё, что зависит от ОС, собрано здесь:

  * каким браузером открывать вход в VK (Edge/Chrome в Windows, встроенный
    Chromium или системный Chrome/Chromium в Linux);
  * аргументы запуска Chromium: под root и в контейнере нужен --no-sandbox,
    а маленький /dev/shm лечится --disable-dev-shm-usage;
  * есть ли графическая сессия (на сервере без X окно входа открыть нельзя —
    нужен xvfb-run или вход по токену);
  * где взять адрес системного прокси (реестр Windows, переменные окружения
    и gsettings в Linux);
  * права на файл сессии (на Linux 0600 — доступ к аккаунту только владельцу).

Модуль не тянет playwright и не зависит от остальных модулей src/ — его можно
импортировать откуда угодно, в том числе из тестов без сети и браузера.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = os.name == "posix"

# Браузеры, которые ищем в PATH и в стандартных папках установки.
BROWSER_NAMES = (
    "msedge",
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def os_label() -> str:
    """Человеческое имя системы — для сообщений в логе."""
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    return sys.platform


def is_root() -> bool:
    """True — процесс запущен от root (sudo). Chromium просит --no-sandbox."""
    if not IS_POSIX:
        return False
    getuid = getattr(os, "geteuid", None)
    if getuid is None:  # pragma: no cover - не POSIX
        return False
    try:
        return getuid() == 0
    except OSError:  # pragma: no cover - экзотика
        return False


def has_display() -> bool:
    """Есть ли графическая сессия: нужно для видимого окна браузера."""
    if IS_WINDOWS or IS_MACOS:
        return True
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "TERM_PROGRAM"):
        if (os.environ.get(var) or "").strip():
            return True
    return False


def can_open_browser() -> bool:
    """Можно ли открывать окно браузера (в т.ч. панель) на этой машине."""
    return has_display()


def headed_login_hint() -> str:
    """Что делать, если видимый вход просят на сервере без графики."""
    return (
        "нет графической сессии (DISPLAY не задан) — окно входа открыть негде. "
        "Варианты для сервера: 1) вход по токену: строка LOGIN:PASS:TOKEN (vk1.a.…) "
        "в data/accounts.txt; 2) войти на машине с экраном и скопировать "
        "data/session_*.json; 3) поднять виртуальный экран: "
        "xvfb-run -a python3 src/vk_auth.py --login"
    )


def browser_channels() -> list[Optional[str]]:
    """Каналы Playwright по порядку попыток; None = встроенный Chromium.

    Windows: Edge или Chrome (во встроенный Chromium exe-сборки он не попадает).
    macOS: Chrome, затем Edge, затем встроенный Chromium.
    Linux/сервер: сначала встроенный Chromium (его ставит `playwright install
    chromium` при запуске панели), затем системные Chrome/Chromium — так вход
    работает и там, где Edge с Chrome не бывает в принципе.
    """
    if IS_WINDOWS:
        return ["msedge", "chrome", None]
    if IS_MACOS:
        return ["chrome", "msedge", None]
    return [None, "chrome", "chromium", "msedge"]


def system_browser_paths() -> list[Path]:
    """Пути к уже установленным браузерам — запасной запуск по executable_path."""
    out: list[Path] = []
    if IS_WINDOWS:
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            out += [
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
    elif IS_MACOS:
        out += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            out.append(Path(found))

    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def installed_browsers() -> set[str]:
    """Какие системные браузеры реально стоят: {"msedge", "chrome", "chromium"}.

    Нужно, чтобы вход не начинался с ошибки «Chromium distribution 'msedge' is not
    found at C:\\…\\msedge.exe» на машине без Edge: каналы, которых нет, уходят
    в конец списка попыток.
    """
    found: set[str] = set()
    for path in system_browser_paths():
        name = path.name.lower()
        if "msedge" in name or "microsoft edge" in str(path).lower():
            found.add("msedge")
        elif "chromium" in name:
            found.add("chromium")
        elif "chrome" in name:
            found.add("chrome")
    return found


def browser_not_installed(error: object) -> bool:
    """True — браузер не установлен (а не «не запустился»: права, прокси, графика)."""
    text = str(error).lower()
    return (
        "is not found at" in text
        or "executable doesn't exist" in text
        or "no such file or directory" in text
    )


def browser_launch_attempts() -> list[dict[str, str]]:
    """Попытки запуска Chromium по порядку: сначала то, что реально стоит.

    Каждый элемент — kwargs для `pw.chromium.launch(...)` или
    `launch_persistent_context(...)` без args/headless: их добавляет вызывающий.
    """
    installed = installed_browsers()
    channels = browser_channels()
    ordered = [c for c in channels if c is None or c in installed]
    ordered += [c for c in channels if c is not None and c not in installed]
    attempts: list[dict[str, str]] = [{"channel": c} if c else {} for c in ordered]
    paths = system_browser_paths()
    # Существующие пути — раньше несуществующих: битый путь не должен съедать попытку.
    attempts.extend({"executable_path": str(path)} for path in paths if path.exists())
    attempts.extend({"executable_path": str(path)} for path in paths if not path.exists())
    return attempts


def browser_args(extra: Optional[Iterable[str]] = None) -> list[str]:
    """Аргументы Chromium с поправками на ОС и на запуск под root."""
    if extra is None:
        args = ["--disable-blink-features=AutomationControlled"]
    else:
        args = list(extra)
    if IS_LINUX:
        if is_root() and "--no-sandbox" not in args:
            args.append("--no-sandbox")  # root без песочницы: иначе Chromium не стартует
        if "--disable-dev-shm-usage" not in args:
            args.append("--disable-dev-shm-usage")  # в контейнере /dev/shm часто 64 МБ
    return args


def browser_missing_hint(reason: object = "", action: str = "повторите вход") -> str:
    """Понятный текст, когда ни один браузер не запустился."""
    tail = f" Причина: {reason}" if reason else ""
    if IS_WINDOWS:
        return (
            "не найден Microsoft Edge или Google Chrome для входа. "
            f"Установите Edge (он есть в Windows 10/11) или Chrome и {action}." + tail
        )
    if IS_MACOS:
        return (
            "не найден Google Chrome или Microsoft Edge для входа. "
            f"Поставьте Chrome (https://www.google.com/chrome/) и {action}." + tail
        )
    return (
        "не удалось открыть Chromium. Поставьте браузер: "
        "python3 -m playwright install chromium (на сервере ещё системные библиотеки: "
        "sudo python3 -m playwright install --with-deps chromium, либо sudo apt install chromium) "
        f"и {action}." + tail
    )


def private_file(path: os.PathLike | str) -> None:
    """Файл только для владельца (Linux/macOS): там лежат сессии и токены."""
    if not IS_POSIX:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # права выставим при следующей записи — из-за этого вход не роняем


def _env_proxy() -> str:
    """Прокси из окружения: обычно так его задают на сервере."""
    for name in PROXY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _gsettings(schema: str, key: str) -> str:
    """Одно значение из настроек GNOME; пустая строка, если gsettings нет."""
    gsettings = shutil.which("gsettings")
    if not gsettings:
        return ""
    try:
        done = subprocess.run(
            [gsettings, "get", schema, key],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or "").strip().strip("'\"")


def _linux_proxy() -> str:
    """Прокси из окружения, иначе из настроек GNOME (gsettings)."""
    env = _env_proxy()
    if env:
        return env
    if _gsettings("org.gnome.system.proxy", "mode") not in {"manual", "auto"}:
        return ""
    host = _gsettings("org.gnome.system.proxy.http", "host")
    port = _gsettings("org.gnome.system.proxy.http", "port")
    if host and port:
        return f"{host}:{port}"
    return _gsettings("org.gnome.system.proxy", "autoconfig-url")


def _windows_proxy() -> str:
    """Системный прокси Windows из реестра (VPN/TUN ломает запросы)."""
    try:
        import winreg  # noqa: PLC0415 - существует только в Windows
    except ImportError:  # pragma: no cover - не Windows
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
            try:
                auto = winreg.QueryValueEx(key, "AutoConfigURL")[0]
            except OSError:
                auto = ""
    except OSError:
        return ""
    if enabled and server:
        return str(server)
    return str(auto or "")


def system_proxy() -> str:
    """Адрес системного прокси, если он включён (влияет на Chromium и curl)."""
    if IS_WINDOWS:
        return _windows_proxy()
    if IS_LINUX:
        return _linux_proxy()
    return _env_proxy()


def proxy_warning(proxy: str) -> str:
    """Подпись про системный прокси для отчёта check_proxy."""
    if IS_WINDOWS:
        where = "в Windows включён системный прокси"
    elif IS_LINUX:
        where = "включён системный прокси (переменные окружения или настройки GNOME)"
    else:
        where = "включён системный прокси (переменные окружения)"
    return (
        f"внимание: {where} {proxy} — трафик (в т.ч. Chromium и curl) идёт через VPN/TUN; "
        "RU-прокси с авторизацией по IP могут молча дропать такие запросы"
    )
