#!/usr/bin/env python3
"""VK Feed Parser — меню для парсинга хештегов VK.

Запуск:
    python app.py               # интерактивное меню
    python app.py 3             # сразу пункт 3 (проверка сессии)
    python app.py 4 --hours 6   # пункт 4 с аргументами (передаются run_tags)
    python app.py --gui         # окно массового запуска (аккаунты, прогресс, Excel)
    python app.py --panel       # панель в браузере (то же, но без tkinter)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC_DIR))

from paths import (  # noqa: E402
    ACCOUNTS_FILE,
    EXPORTS_DIR,
    SESSION_FILE,
    TAGS_FILE,
    ensure_layout,
)
from settings import load_settings, save_settings  # noqa: E402

TITLE = """
╔══════════════════════════════════════════════════════════╗
║   VK Feed Parser — поиск постов по хештегам (newsfeed)   ║
╚══════════════════════════════════════════════════════════╝"""

MENU = """
 1. Авторизация VK (браузер: телефон + пароль, капчу решаете сами)
 2. Вход по QR-коду (быстро, без капчи — сканирует телефон)
 3. Проверить сессию и тестовый поиск
 4. Запустить обход тегов (многопоточно)
 5. Проба одного тега
 6. Выгрузить найденное в Excel
 7. Состояние базы и тегов
 8. Настройки (потоки, страницы, проходы)
 9. Показать файлы данных (теги, аккаунты, прокси)
10. Массовый запуск — окно с аккаунтами и прогрессом
11. Панель в браузере — то же самое, но красиво и без tkinter
 0. Выход
"""


def run_script(name: str, extra: list[str]) -> int:
    """Запускает модуль из src/ отдельным процессом."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, f"--{name}", *extra]
    else:
        cmd = [sys.executable, str(SRC_DIR / f"{name}.py"), *extra]
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.call(cmd, cwd=str(Path(__file__).resolve().parent))
    except KeyboardInterrupt:
        print("\nпрервано пользователем")
        return 130


def show_files() -> None:
    for label, path in (
        ("теги", TAGS_FILE),
        ("аккаунты", ACCOUNTS_FILE),
        ("прокси", Path(__file__).resolve().parent / "proxies.txt"),
        ("сессия", SESSION_FILE),
        ("экспорт", EXPORTS_DIR),
    ):
        exists = "есть" if path.exists() else "нет"
        print(f"  {label:<10} [{exists}] {path}")
    print()
    print("  Чтобы добавить теги — допишите строки в файл тегов (по одному на строку).")
    print("  Аккаунты: телефон;пароль;2FA;прокси")


def ask(prompt: str, default: str = "") -> str:
    try:
        value = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def settings_menu() -> None:
    settings = load_settings()
    print("\nТекущие настройки:")
    for key, value in settings.items():
        print(f"  {key:<28} {value}")
    print("\nEnter — оставить как есть.")
    fields = {
        "max_accounts": "одновременных потоков (аккаунтов)",
        "page_count": "постов на страницу (10..200)",
        "max_pages_per_tag": "макс. страниц на окно",
        "repeat_passes": "повторных проходов по тегу",
        "repeat_pause_sec": "пауза между проходами, с",
        "request_pause_sec": "пауза между запросами, с",
        "export_interval_sec": "живой экспорт каждые, с",
        "stop_when_no_new_pages": "стоп после N страниц без новых",
        "default_days": "период по умолчанию, суток",
        "window_hours": "размер окна времени, ч",
        "job_attempts": "попыток на окно при ошибках",
    }
    for key, label in fields.items():
        raw = ask(f"  {label} [{settings.get(key)}]: ")
        if not raw:
            continue
        try:
            settings[key] = float(raw) if "." in raw else int(raw)
        except ValueError:
            print("   не число — пропущено")
    save_settings(settings)
    print("сохранено:", json.dumps(load_settings(), ensure_ascii=False))


def handle(choice: str, extra: list[str]) -> bool:
    """True — продолжать цикл меню."""
    if choice == "1":
        run_script("vk_auth", ["--login", *extra])
    elif choice == "2":
        run_script("vk_auth", ["--login", "--qr", *extra])
    elif choice == "3":
        run_script("vk_auth", ["--check", *extra])
    elif choice == "4":
        run_script("run_tags", extra)
    elif choice == "5":
        tag = extra[0] if extra else ask("  Тег (без решётки): ")
        if tag:
            run_script("probe", [tag, *extra[1:]])
    elif choice == "6":
        run_script("export_xlsx", extra)
    elif choice == "7":
        run_script("state_db", ["--stats", "--tags", *extra])
    elif choice == "8":
        settings_menu()
    elif choice == "9":
        show_files()
    elif choice in ("10", "gui", "окно"):
        run_script("gui", extra)
    elif choice in ("11", "panel", "панель"):
        run_script("webui", extra)
    elif choice in ("0", "q", "exit", "выход"):
        return False
    else:
        print("  нет такого пункта")
    return True


def main() -> int:
    ensure_layout()
    args = sys.argv[1:]
    print(TITLE)
    if args and args[0] in {"--gui", "gui"}:
        # Окно поднимаем в этом же процессе: в exe-сборке повторный запуск
        # app.py с флагом --gui зациклился бы.
        import gui

        return gui.main(args[1:])
    if args and args[0] in {"--panel", "panel", "web"}:
        # Панель в браузере: tkinter не нужен вовсе.
        import webui

        return webui.main(args[1:])
    if args and args[0] in {str(i) for i in range(0, 12)}:
        handle(args[0], args[1:])
        return 0

    while True:
        print(MENU)
        try:
            choice = input("Выберите пункт: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not choice:
            continue
        if not handle(choice, []):
            return 0
        print()
        input("Enter — вернуться в меню…")


if __name__ == "__main__":
    raise SystemExit(main())
