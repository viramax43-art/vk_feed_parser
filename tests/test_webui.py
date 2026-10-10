#!/usr/bin/env python3
"""Проверка браузерной панели: страница, API, файлы и запуск процесса.

    python tests/test_webui.py

Сеть и браузер не нужны: панель поднимается на свободном порту, редактируемые файлы
(tags.txt, accounts.txt, proxies.txt) и файл настроек подменяются временными —
настоящие данные в data/ не портим, — а прогон и вход в VK вместо src/run_tags.py и
src/vk_auth.py уходят в заглушку: проверяем команду, которую собрала панель.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import webui  # noqa: E402
import settings as settings_mod  # noqa: E402
from paths import ACCOUNTS_FILE, TAGS_FILE  # noqa: E402
from settings import CONFIG_FILE  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    """Запрос к API панели: код ответа + разобранный JSON."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def page(path: str) -> str:
    with urlrequest.urlopen(base + path, timeout=15) as response:
        return response.read().decode("utf-8")


# 1. панель поднимается на свободном порту, чужие файлы подменяем
tmp = Path(tempfile.mkdtemp(prefix="webui_"))
for name in ("tags.txt", "accounts.txt", "proxies.txt"):
    (tmp / name).write_text(f"# {name}\n", encoding="utf-8")
# Настройки панель пишет в data/settings.json — на время проверки уводим файл в tmp.
real_config = settings_mod.CONFIG_FILE
settings_mod.CONFIG_FILE = tmp / "settings.json"
spawned: list[list] = []


class FakeRunner(webui.Runner):
    """Прогон не запускаем: запоминаем команду и делаем вид, что стартовали."""

    def start(self, script: str, extra: list[str], label: str) -> tuple[bool, str]:
        spawned.append([script, list(extra), label])
        return True, ""


port = webui.pick_port(webui.DEFAULT_PORT + 501)
httpd, _real_runner = webui.build_server(port)
httpd.RequestHandlerClass.runner = FakeRunner()
editable_before = dict(webui.EDITABLE)
webui.EDITABLE = {name[:-4]: tmp / name for name in ("tags.txt", "accounts.txt", "proxies.txt")}
webui.ACCOUNTS_FILE = tmp / "accounts.txt"
base = f"http://{webui.HOST}:{port}"
threading.Thread(target=httpd.serve_forever, daemon=True).start()

tags_before = TAGS_FILE.read_bytes()
accounts_before = ACCOUNTS_FILE.read_bytes()
config_before = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else b""
live: webui.Runner | None = None

try:
    html = page("/")
    check("VK Feed Parser" in html and "app.js" in html, "страница отдаётся и подключает app.js")
    check("Старт" in html and "Стоп" in html, "на странице есть кнопки «Старт» и «Стоп»")
    check(all(tab in html for tab in ("Прогон", "Аккаунты", "Теги", "События")), "на странице все четыре раздела")
    check(len(page("/style.css")) > 1000, "стили отдаются")
    check(len(page("/app.js")) > 1000, "скрипт панели отдаётся")
    check("Войти в VK" in page("/app.js"), "в таблице аккаунтов есть кнопка «Войти в VK»")

    # 2. карточки и таблицы
    status, data = call("/api/status")
    check(status == 200 and data["port"] == port, f"status знает свой порт: {data.get('port')}")
    check(set(data["files"]) == {"tags", "accounts", "proxies"}, "status перечисляет три файла данных")
    check(len(data["periods"]) >= 3, f"периоды для кнопок («24 часа» и пр.) есть: {len(data['periods'])}")

    code, progress = call("/api/progress")
    check(code == 200 and int(progress["posts_total"]) >= 0, f"прогресс читается: постов {progress['posts_total']}")
    check(progress["state"] == "ожидание", f"без прогона состояние «ожидание»: {progress['state']}")
    for key in ("accounts_ready", "accounts_hold", "accounts_dead", "tags_pct"):
        check(key in progress, f"в прогресс попал {key}")

    for route in ("/api/accounts", "/api/tags", "/api/events"):
        code, payload = call(route)
        check(code == 200 and isinstance(payload["rows"], list), f"{route} отдаёт список строк")

    # 3. файлы правятся, но чужие не трогаются
    code, payload = call("/api/files/tags")
    check(code == 200 and payload["content"] == "# tags.txt\n", "GET /api/files/tags читает файл")
    code, payload = call("/api/files/tags", "PUT", {"content": "# тег\n#панель\n"})
    check(code == 200 and payload["ok"], f"PUT /api/files/tags сохраняет: {payload.get('message')}")
    check((tmp / "tags.txt").read_text(encoding="utf-8") == "# тег\n#панель\n", "сохранился именно файл из панели")
    check(TAGS_FILE.read_bytes() == tags_before, "настоящий data/tags.txt не тронут")
    code, payload = call("/api/files/no-such-file", "PUT", {"content": "x"})
    check(code == 404 and not payload["ok"], "чужой файл через API не записать")


    # 4. теги и аккаунты
    code, payload = call("/api/tags/add", "POST", {"content": "маникюр", "tags_file": str(tmp / "add.txt")})
    check(payload["ok"] and (tmp / "add.txt").read_text(encoding="utf-8") == "маникюр\n", "новый тег дописан в файл")
    code, payload = call("/api/tags/add", "POST", {"content": "маникюр", "tags_file": str(tmp / "add.txt")})
    check(payload["ok"] and "уже в файле" in payload["message"], f"повторный тег не дублируется: {payload['message']}")
    code, payload = call(
        "/api/tags/add",
        "POST",
        {"content": "# комментарий\n// заметка", "tags_file": str(tmp / "add.txt")},
    )
    check(not payload["ok"], f"комментарии за теги не считаются: {payload['message']}")
    check((tmp / "add.txt").read_text(encoding="utf-8") == "маникюр\n", "после комментариев файл тегов не изменился")
    code, payload = call("/api/tags/reset", "POST", {"query": ""})
    check(not payload["ok"], "сброс без тега отклоняется")

    code, payload = call("/api/accounts/add", "POST", {"content": "# только комментарий\n// и это\n   "})
    check(not payload["ok"] and (tmp / "accounts.txt").read_text(encoding="utf-8") == "# accounts.txt\n",
          "мусор в аккаунты не попал")
    code, payload = call("/api/accounts/add", "POST", {"content": "+79990000001;пароль;;"})
    check(payload["ok"], f"нормальная строка аккаунта принимается: {payload['message']}")
    check("+79990000001" in (tmp / "accounts.txt").read_text(encoding="utf-8"), "строка аккаунта дописана")

    # 5. настройки
    code, settings = call("/api/settings")
    check(code == 200 and "max_accounts" in settings, "настройки читаются")
    code, payload = call("/api/settings", "PUT", {"max_accounts": settings["max_accounts"]})
    check(payload["ok"], f"настройки сохраняются: {payload.get('message')}")
    check((tmp / "settings.json").exists(), "настройки легли в файл настроек, а не в чужой")

    # 6. кнопка «Старт» собирает правильную команду
    code, payload = call(
        "/api/run",
        "POST",
        {
            "hours": 6,
            "passes": 3,
            "workers": 4,
            "validate_first": True,
            "full": True,
            "tags_file": str(tmp / "tags.txt"),
        },
    )
    check(payload["ok"], f"«Старт» отвечает без ошибки: {payload.get('message')}")
    check(len(spawned) == 1 and spawned[0][0] == "run_tags", f"запускается run_tags: {spawned}")
    flags = spawned[0][1] if spawned else []
    for want in (["--hours", "6"], ["--passes", "3"], ["--workers", "4"], ["--validate-first"], ["--full"]):
        check(all(part in flags for part in want), "в команде есть " + " ".join(want))
    check(flags[-1] == str(tmp / "tags.txt"), "файл тегов передан последним аргументом")
    check(webui.load_settings()["max_accounts"] == 4, "потоки и проходы из панели легли в настройки")

    # 6.5. «Войти в VK»: вход идёт для нужного аккаунта
    code, payload = call("/api/accounts/add", "POST", {"content": "+79990000002;пароль;;"})
    check(
        payload["ok"] and "всего аккаунтов 2" in payload["message"],
        f"в файле аккаунтов стало два: {payload['message']}",
    )
    code, payload = call("/api/accounts")
    check(all("number" in row for row in payload["rows"]), "у строк аккаунтов есть номер для входа")
    code, payload = call("/api/login", "POST", {"account": 2})
    check(payload["ok"] and spawned[-1][0] == "vk_auth", f"«Войти в VK» запускает вход: {payload.get('message')}")
    check(spawned[-1][1][:3] == ["--login", "--account", "2"], f"вход во второй аккаунт: {spawned[-1][1]}")
    check(
        "--profile" in spawned[-1][1] and spawned[-1][1][-1].endswith("vk_browser_profile_2"),
        f"у второго аккаунта свой профиль браузера: {spawned[-1][1][-1]}",
    )
    code, payload = call("/api/login", "POST", {"account": 0})
    check(not payload["ok"] and "нет в accounts.txt" in payload["message"], f"чужой номер не пускаем: {payload['message']}")

    # 7. «Стоп» посылает Ctrl+C живому процессу
    live = webui.Runner()
    ok, message = live.spawn(
        [sys.executable, "-u", "-c", "import time\nprint('пошёл', flush=True)\ntime.sleep(30)"],
        "тест стопа",
    )
    check(ok, f"процесс запустился: {message}")
    ok, message = live.spawn([sys.executable, "-c", "print(1)"], "второй")
    check(not ok and "уже идёт" in message, f"два процесса панель не запускает: {message}")
    deadline = time.time() + 10
    while time.time() < deadline:
        if any("пошёл" in line for _seq, line in live._lines):
            break
        time.sleep(0.1)
    ok, message = live.stop()
    check(ok, f"«Стоп» принял команду: {message}")
    deadline = time.time() + 10
    while live.running and time.time() < deadline:
        time.sleep(0.1)
    check(not live.running, "процесс остановлен")
    lines = [line for _seq, line in live._lines]
    check(any("пошёл" in line for line in lines), "вывод процесса попал в лог панели")
    check(any("останавливаю" in line for line in lines), "в логе видно, что нажали «Стоп»")
    code, payload = call("/api/stop", "POST", {})
    check(not payload["ok"], f"«Стоп» без прогона честно отказывает: {payload['message']}")

    # 8. лог панели, чужие адреса и кнопки без прогона
    code, payload = call("/api/logs?after=0")
    check(isinstance(payload["lines"], list) and "next" in payload, "лог отдаётся вместе с курсором")
    code, payload = call("/api/logs?after=999999")
    check(payload["lines"] == [], "старые строки лога повторно не приходят")
    code, payload = call("/api/nothing")
    check(code == 404, "неизвестный маршрут отвечает 404")
    code, payload = call("/api/open", "POST", {"target": "нечто"})
    check(not payload["ok"], "неизвестную папку панель не открывает")
    code, payload = call("/api/export", "POST", {})
    check(payload["ok"], "кнопка выгрузки в Excel запускается отдельным процессом")
finally:
    httpd.shutdown()
    httpd.server_close()
    if live is not None and live.running:
        live.stop()
    webui.EDITABLE = editable_before
    webui.ACCOUNTS_FILE = ACCOUNTS_FILE
    settings_mod.CONFIG_FILE = real_config
    shutil.rmtree(tmp, ignore_errors=True)

# 9. настоящие файлы данных не пострадали
check(TAGS_FILE.read_bytes() == tags_before, "data/tags.txt остался как был")
check(ACCOUNTS_FILE.read_bytes() == accounts_before, "data/accounts.txt остался как был")
config_now = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else b""
check(config_now == config_before, "data/settings.json остался как был")
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ПАНЕЛЬ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)
