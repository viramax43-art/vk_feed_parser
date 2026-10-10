#!/usr/bin/env python3
"""Корень проекта: рядом с .py в разработке, рядом с .exe в сборке."""
from __future__ import annotations

import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_src_dir() -> Path:
    if getattr(sys, "frozen", False):
        # В onedir-сборке исходники лежат в _internal; для import хватает sys.path.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return get_base_dir()
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
SRC_DIR = get_src_dir()
DATA_DIR = BASE_DIR / "data"


def force_utf8_stdio() -> None:
    """Консоль Windows в cp1251 — ломает русский лог. Переводим вывод в UTF-8."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


force_utf8_stdio()

TAGS_FILE = DATA_DIR / "tags.txt"
ACCOUNTS_FILE = DATA_DIR / "accounts.txt"
PROXIES_FILE = BASE_DIR / "proxies.txt"

# VK: одна сессия = cookies браузера (+ полученный из них user token).
SESSION_FILE = DATA_DIR / "session.json"
PROFILE_DIR = DATA_DIR / "vk_browser_profile"
LOG_DIR = DATA_DIR / "logs"
EXPORTS_DIR = DATA_DIR / "exports"

STATE_DB = DATA_DIR / "parser.sqlite3"


def resolve_proxies_file() -> Path:
    """proxies.txt рядом с exe или в data/ — что реально заполнено."""
    candidates = [BASE_DIR / "proxies.txt", DATA_DIR / "proxies.txt"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("//"):
                    return path
        except OSError:
            continue
    return PROXIES_FILE


def ensure_layout() -> None:
    """Создаёт data/ и пустые конфиги при первом запуске."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if not TAGS_FILE.exists():
        TAGS_FILE.write_text(
            "// один тег на строку, решётка необязательна\n"
            "маникюр\n"
            "ремонтквартир\n",
            encoding="utf-8",
        )
    if not ACCOUNTS_FILE.exists():
        ACCOUNTS_FILE.write_text(
            "# телефон;пароль;2FA_СЕКРЕТ;прокси (host:port:user:pass)\n"
            "# логин = номер телефона в формате +7XXXXXXXXXX\n"
            "# прокси необязателен: если пусто — берётся прокси из proxies.txt\n",
            encoding="utf-8",
        )
    if not PROXIES_FILE.exists():
        PROXIES_FILE.write_text(
            "# host:port:user:pass — по одной строке на аккаунт\n"
            "# Также: login:password@ip:port или http://user:pass@host:port\n",
            encoding="utf-8",
        )


def invoke_cmd(script: str) -> list:
    """Команда запуска модуля из src/: python file.py или exe --file."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{script}"]
    return [sys.executable, str(SRC_DIR / f"{script}.py")]
