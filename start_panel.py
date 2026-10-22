#!/usr/bin/env python3
"""Запуск панели «в один клик»: окружение, зависимости, браузер.

    python3 start_panel.py              # поднять панель (первый раз может создать .venv)
    python3 start_panel.py --port 9000  # другой порт
    python3 start_panel.py --host 0.0.0.0   # сервер: панель доступна по сети
    python3 start_panel.py --skip-browser-install   # не проверять Chromium

Скрипт нужен обычному человеку: он сам найдёт Python с нужными пакетами, при
необходимости поставит их в .venv, потом запустит src/webui.py — панель в браузере.

Windows: двойной клик по «Запустить панель.bat».
macOS: двойной клик по «Запустить панель.command».
Linux/сервер: bash start_panel.sh (перед первым запуском: chmod +x start_panel.sh).
Скрипт сам определяет систему: путь к python в .venv (Scripts/python.exe или bin/python),
поиск интерпретатора (py/ python3.x) и подсказки по системным библиотекам Chromium.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
# Метка «Chromium уже проверен». Лежит в корне проекта, а не в .venv: браузер
# playwright кэшируется на пользователя, а папки .venv при запуске может не быть.
BROWSER_STAMP = BASE_DIR / ".browser-ok"
NEEDED = ("curl_cffi", "playwright", "xlsxwriter")
SUPPORTED_MIN = (3, 9)  # минимальная версия Python для проекта
SUPPORTED_MAX = (3, 12)  # выше — колёса curl_cffi 0.7.4 могут не собраться


def say(message: str) -> None:
    print(message, flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def candidates() -> list[list[str]]:
    """Кого пробовать: текущий Python, PATH и сборки с python.org."""
    out: list[list[str]] = [[sys.executable]]
    if os.name == "nt":
        out.extend([["py", "-3.12"], ["py", "-3.11"], ["py", "-3.10"], ["py", "-3"]])
    for name in ("python3.12", "python3.11", "python3.10", "python3.13", "python3.9", "python3"):
        path = shutil.which(name)
        if path:
            out.append([path])
    for pattern in (
        "/Library/Frameworks/Python.framework/Versions/*/bin/python3",
        "/usr/local/bin/python3.1*",
        str(Path.home() / ".local/bin/python3.1*"),
    ):
        out.extend([[item] for item in sorted(glob.glob(pattern), reverse=True)])

    unique: list[list[str]] = []
    seen: set[str] = set()
    for cmd in out:
        key = " ".join(cmd)
        if key not in seen:
            seen.add(key)
            unique.append(cmd)
    return unique


def run_capture(cmd: list[str], code: str) -> Optional[str]:
    try:
        done = subprocess.run([*cmd, "-c", code], capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def deps_ok(cmd: list[str]) -> bool:
    """True — интерпретатор уже умеет curl_cffi + playwright + xlsxwriter."""
    return run_capture(cmd, "import " + ", ".join(NEEDED)) is not None


def python_version(cmd: list[str]) -> Optional[tuple[int, int]]:
    raw = run_capture(cmd, "import sys; print('%d.%d' % sys.version_info[:2])")
    if not raw:
        return None
    try:
        major, minor = raw.split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def install_chromium(cmd: list[str]) -> None:
    """Chromium нужен парсеру; без него панель работает, но сбор не пойдёт."""
    if BROWSER_STAMP.exists():
        return
    say("Проверяю браузер Chromium для парсера (в первый раз может качаться пару минут)…")
    try:
        code = subprocess.call([*cmd, "-m", "playwright", "install", "chromium"])
    except OSError as exc:
        code = 1
        say(f"playwright не запустился: {exc}")
    if code == 0:
        try:
            BROWSER_STAMP.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass  # метку записать не вышло — не беда, панель всё равно поднимаем
        return
    say("Chromium не установился (нет интернета?). Потом можно повторить:")
    say(f"  {' '.join(cmd)} -m playwright install chromium")
    if os.name != "nt":
        # На сервере браузеру нужны системные библиотеки, а под root — --no-sandbox
        # (его проект добавляет сам).
        say("  Linux/сервер: sudo python3 -m playwright install --with-deps chromium")
        say("  или системный браузер: sudo apt install chromium (проект найдёт его сам)")


def usable(cmd: list[str]) -> bool:
    """Интерпретатор подходит как база для зависимостей проекта."""
    version = python_version(cmd)
    return version is not None and SUPPORTED_MIN <= version <= SUPPORTED_MAX


def ensure_python() -> list[str]:
    """Интерпретатор с нужными пакетами: готовый из системы или .venv проекта."""
    skip_browser = "--skip-browser-install" in sys.argv

    for cmd in candidates():
        if deps_ok(cmd):
            say(f"Использую Python с пакетами: {' '.join(cmd)}")
            if not skip_browser:
                install_chromium(cmd)
            return cmd

    base = [sys.executable]
    for cmd in candidates():
        if usable(cmd):
            base = cmd
            break
    version = python_version(base)
    say(
        "Готового Python с пакетами не нашёл — делаю отдельное окружение .venv"
        + (f" (Python {version[0]}.{version[1]})" if version else "")
        + "."
    )
    python = venv_python()
    if not python.exists():
        if subprocess.call([*base, "-m", "venv", str(VENV_DIR)]) != 0:
            raise SystemExit(
                "Не получилось создать .venv.\n"
                "Поставьте Python 3.11 или 3.12 с https://www.python.org/downloads/ и запустите снова."
            )
    venv_cmd = [str(python)]
    if not deps_ok(venv_cmd):
        say("Ставлю зависимости (pip install -r requirements.txt) — нужен интернет.")
        subprocess.call([*venv_cmd, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
        code = subprocess.call(
            [*venv_cmd, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")]
        )
        if code != 0 or not deps_ok(venv_cmd):
            raise SystemExit(
                "Зависимости не поставились. Проверьте интернет и повторите вручную:\n"
                f"  {' '.join(venv_cmd)} -m pip install -r requirements.txt"
            )
    if not skip_browser:
        install_chromium(venv_cmd)
    return venv_cmd


def display_note() -> None:
    """Сервер без графики: панель поднимется, но вход в браузере требует экрана."""
    if os.name == "nt" or sys.platform == "darwin":
        return
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or "").strip():
        return
    say("Графической сессии нет (сервер или ssh) — это нормально для панели:")
    say("  панель открывайте по адресу из строки ниже (или пробросьте порт: ssh -L 8787:127.0.0.1:8787 …),")
    say("  а вход в VK делайте токеном LOGIN:PASS:TOKEN в data/accounts.txt или так:")
    say("  xvfb-run -a python3 src/vk_auth.py --login")


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--skip-browser-install"]
    python = ensure_python()
    say("Запускаю панель… браузер откроется сам, адрес будет в строке ниже.")
    display_note()
    return subprocess.call([*python, str(BASE_DIR / "src" / "webui.py"), *args])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
