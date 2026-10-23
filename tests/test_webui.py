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

import accounts as accounts_mod  # noqa: E402
import paths as paths_mod  # noqa: E402
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
# Сессии из TOKEN и настройки — тоже в tmp, чтобы не трогать настоящий data/.
real_data_dir = paths_mod.DATA_DIR
real_accounts_data = accounts_mod.DATA_DIR
paths_mod.DATA_DIR = tmp
accounts_mod.DATA_DIR = tmp
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
# Лог панели — тоже в tmp: тест не должен писать в настоящий data/logs/.
real_log_dir = webui.LOG_DIR
webui.LOG_DIR = tmp / "logs"
# База панели — тоже временная: тест не читает и не пишет настоящую data/parser.sqlite3,
# а статусы аккаунтов (например «мёртв — протух токен») готовятся прямо здесь.
panel_db_before = webui.panel_db
panel_db = webui.DB(tmp / "parser.sqlite3")
webui.panel_db = lambda: panel_db
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
    # «Работать, ч» — сколько часов процесс занят, считая от «Старт». Поле заполняется
    # по периоду прямо в браузере: иначе «за 6 часов» уходило на сервер с нулём часов
    # работы, и прогон заканчивался через минуту (период-то уже собран).
    panel_js = page("/app.js")
    check(
        "Работать, ч" in html and 'id="run-duty"' in html and 'id="run-pause"' in html,
        "в форме прогона есть «Работать, ч» и «Пауза, с»",
    )
    check(
        "syncDutyFromPeriod" in panel_js
        and 'addEventListener("change", syncDutyFromPeriod)' in panel_js
        and "AUTO_DUTY_MAX_HOURS" in panel_js,
        "«Работать, ч» подставляется по периоду (и не больше суток по умолчанию)",
    )
    check(
        "«Работать, ч» = 0" in panel_js,
        "в подсказке раздела сказано, что 0 — это один проход",
    )
    # В строке тега две кнопки: «В очередь» (состояние и курсор) и «Обнулить счётчики»
    # (только накопленные Постов/Страниц/Проходов) — раньше счётчики обнулить было нечем.
    check(
        "data-zero" in panel_js and '"/api/tags/reset-stats"' in panel_js,
        "в таблице тегов есть кнопка «Обнулить счётчики» рядом с «В очередь»",
    )

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
    for key in ("posts_since_reset", "tag_links_since_reset", "counter_reset_at"):
        check(key in progress, f"карточка «Постов в базе» получает {key}")

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
    # Счётчики тега («Постов», «Страниц») — накопительные: «В очередь» их не сбрасывает,
    # поэтому у панели отдельная кнопка «Обнулить счётчики».
    panel_db.bump_tag("маникюр", items=120, pages=4, next_from="курсор", pass_no=2)
    code, payload = call("/api/tags/reset-stats", "POST", {"query": ""})
    check(not payload["ok"], "обнуление счётчиков без тега отклоняется")
    code, payload = call("/api/tags/reset-stats", "POST", {"query": "маникюр"})
    check(payload["ok"], f"счётчики тега обнуляются: {payload['message']}")
    check(
        "Постов в базе" in payload["message"],
        f"в ответе видно, что карточка «Постов в базе» считает с нуля: {payload['message']}",
    )
    tag_state = panel_db.tag_state("маникюр")
    check(
        int(tag_state["total_items"]) == 0
        and int(tag_state["total_pages"]) == 0
        and int(tag_state["passes_done"]) == 0,
        f"Постов/Страниц/Проходов стали нулями: "
        f"{tag_state['total_items']}/{tag_state['total_pages']}/{tag_state['passes_done']}",
    )
    check(
        tag_state["status"] == "running" and tag_state["next_from"] == "курсор",
        f"обнуление счётчиков не трогает состояние и курсор тега (статус {tag_state['status']})",
    )
    # Отметка отсчёта: карточка «Постов в базе» в панели считает с нуля (ради этого
    # кнопку и нажимают — иначе «счётчики обнулены», а в карточке всё те же 10894).
    code, progress = call("/api/progress")
    check(
        code == 200 and float(progress["counter_reset_at"]) > 0
        and progress["posts_since_reset"] == 0
        and progress["tag_links_since_reset"] == 0,
        f"карточка панели начинается с нуля после обнуления счётчиков: "
        f"{progress.get('posts_since_reset')}/{progress.get('tag_links_since_reset')} "
        f"(отметка {progress.get('counter_reset_at')})",
    )
    check(
        'id="kpi-posts-cap"' in page("/") and "Постов с обнуления" in panel_js,
        "панель меняет подпись карточки, когда идёт отсчёт с обнуления",
    )
    code, payload = call("/api/tags/reset", "POST", {"query": "маникюр"})
    tag_state = panel_db.tag_state("маникюр")
    check(
        payload["ok"] and tag_state["status"] == "pending" and not tag_state["next_from"],
        "«В очередь» по-прежнему возвращает тег к сбору (это отдельная кнопка)",
    )

    code, payload = call("/api/accounts/add", "POST", {"content": "# только комментарий\n// и это\n   "})
    check(not payload["ok"] and (tmp / "accounts.txt").read_text(encoding="utf-8") == "# accounts.txt\n",
          "мусор в аккаунты не попал")
    code, payload = call("/api/accounts/add", "POST", {"content": "+79990000001;пароль;;"})
    check(payload["ok"], f"нормальная строка аккаунта принимается: {payload['message']}")
    check("+79990000001" in (tmp / "accounts.txt").read_text(encoding="utf-8"), "строка аккаунта дописана")
    _code, progress = call("/api/progress")
    check(
        progress["accounts_total"] == 1,
        f"карточка «Аккаунты» считает строки accounts.txt, а не базу: "
        f"всего {progress['accounts_total']}, в базе {progress.get('accounts_db')}",
    )
    check(
        progress["accounts_total"]
        == progress["accounts_ready"] + progress["accounts_hold"]
        + progress["accounts_dead"] + progress["accounts_needs_proxy"],
        f"статусы сходятся с числом аккаунтов: {progress}",
    )
    check(
        progress["accounts_needs_proxy"] == 1,
        f"аккаунт без прокси виден карточке: {progress['accounts_needs_proxy']}",
    )
    code, payload = call("/api/accounts/add", "POST", {"content": "+79990000001;пароль;;\n89990000001;другой;;"})
    check(
        payload["ok"] and ("обновлено 1" in payload["message"] or "уже были 1" in payload["message"]),
        f"дубликат +7/8 не плодится: {payload['message']}",
    )
    acc_after_dup = (tmp / "accounts.txt").read_text(encoding="utf-8")
    check(acc_after_dup.count(";") >= 3 and acc_after_dup.count("\n") <= 4, f"в файле одна рабочая строка: {acc_after_dup!r}")
    check(acc_after_dup.count("+79990000001;") + acc_after_dup.count("89990000001;") == 1, "один логин после слияния 8/+7")
    code, data = call("/api/accounts")
    logins = [row["login"] for row in data["rows"]]
    check("+79990000001" in logins, f"список показывает аккаунт из файла: {logins}")
    token_line = "17702694724:SecretPass:vk1.a.TestTokenValueForUnitTestOnly"
    code, payload = call("/api/accounts/add", "POST", {"content": token_line})
    check(payload["ok"] and "новых" in payload["message"], f"LOGIN:PASS:TOKEN принимается: {payload['message']}")
    acc_text = (tmp / "accounts.txt").read_text(encoding="utf-8")
    check("17702694724;SecretPass;;" in acc_text, "TOKEN-строка нормализована в accounts.txt")
    check("vk1.a.TestTokenValueForUnitTestOnly" not in acc_text, "токен не дублируется в accounts.txt")
    session_path = tmp / "session_17702694724.json"
    check(session_path.exists(), "session_<login>.json создан из TOKEN")
    import json as _json
    sess = _json.loads(session_path.read_text(encoding="utf-8"))
    check(sess.get("access_token") == "vk1.a.TestTokenValueForUnitTestOnly", "в сессии лежит импортированный токен")
    code, data = call("/api/accounts")
    check(any(row["login"] == "17702694724" for row in data["rows"]), "после TOKEN аккаунт виден в таблице")
    code, payload = call("/api/accounts/add", "POST", {"content": token_line})
    check("уже были 1" in payload["message"] or "обновлено" in payload["message"], f"повтор TOKEN не дублирует: {payload['message']}")
    check((tmp / "accounts.txt").read_text(encoding="utf-8").count("17702694724;") == 1, "логин с токеном один раз в файле")

    # 5. настройки
    code, settings = call("/api/settings")
    check(code == 200 and "max_accounts" in settings, "настройки читаются")
    code, payload = call("/api/settings", "PUT", {"max_accounts": settings["max_accounts"]})
    check(payload["ok"], f"настройки сохраняются: {payload.get('message')}")
    check((tmp / "settings.json").exists(), "настройки легли в файл настроек, а не в чужой")

    # 5.1. Выбор «с #» / «без #» (настройка tag_query) страница сохраняет ТЕМ ЖЕ методом,
    # которым маршрут объявлен на сервере. Раньше страница звала POST /api/settings, а
    # сервер знал только PUT: в панели это выглядело как «нет маршрута /api/settings»,
    # и режим тега молча не сохранялся.
    panel_js = page("/app.js")
    save_body = panel_js.split("function saveTagQuery()", 1)[1].split("\n}", 1)[0]
    if "putJson(" in save_body:
        used = "PUT"
    elif "postJson(" in save_body:
        used = "POST"
    else:
        used = ""
    check(used != "", f"выбор «с #» / «без #» сохраняется через API настроек: {used or 'метода нет'}")

    code, payload = call("/api/settings", used or "PUT", {"tag_query": "word"})
    check(
        code == 200 and payload.get("ok") and "настройки сохранены" in payload.get("message", ""),
        f"{used or 'PUT'} /api/settings — маршрут сохранения настроек: {payload.get('message')}",
    )
    check("без # (слово)" in payload.get("message", ""), "панель подтверждает режим «без #»")
    check(
        json.loads((tmp / "settings.json").read_text(encoding="utf-8"))["tag_query"] == "word",
        "режим тега лёг в settings.json (его же прочитает прогон)",
    )

    # Старый app.js мог остаться в браузере: POST тот же маршрут тоже принимает.
    code, payload = call("/api/settings", "POST", {"tag_query": "word"})
    check(
        code == 200 and payload.get("ok"),
        f"POST /api/settings тоже принимается (страница из кеша работает): {payload.get('message')}",
    )

    code, payload = call("/api/settings", used or "PUT", {"tag_query": "hash"})
    check(
        json.loads((tmp / "settings.json").read_text(encoding="utf-8"))["tag_query"] == "hash",
        "режим тега возвращается на «с # (хештег)»",
    )

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
            "no_refresh": True,
            "tags_file": str(tmp / "tags.txt"),
        },
    )
    check(payload["ok"], f"«Старт» отвечает без ошибки: {payload.get('message')}")
    check(len(spawned) == 1 and spawned[0][0] == "run_tags", f"запускается run_tags: {spawned}")
    flags = spawned[0][1] if spawned else []
    for want in (["--hours", "6"], ["--passes", "3"], ["--workers", "4"], ["--validate-first"],
                 ["--full"], ["--no-refresh"]):
        check(all(part in flags for part in want), "в команде есть " + " ".join(want))
    check(flags[-1] == str(tmp / "tags.txt"), "файл тегов передан последним аргументом")
    check(webui.load_settings()["max_accounts"] == 4, "потоки и проходы из панели легли в настройки")

    # 6.2. «Работать, ч» (--run-hours): период — это ЧТО собирать, а сколько работать
    # задаёт это поле (оно считается от «Старт» и заполняется по периоду). Проходы его
    # не обрезают, а пауза между проверками новых постов берётся из поля как есть.
    before = len(spawned)
    code, payload = call(
        "/api/run",
        "POST",
        {
            "hours": 6,
            "run_hours": 6,
            "passes": 3,
            "repeat_pause": 30,
            "workers": 4,
            "tags_file": str(tmp / "tags.txt"),
        },
    )
    check(payload["ok"], f"прогон на время стартует: {payload.get('message')}")
    duty_flags = spawned[before][1] if len(spawned) > before else []
    for want in (["--hours", "6"], ["--run-hours", "6"], ["--repeat-pause", "30"]):
        check(all(part in duty_flags for part in want), "в команде на время есть " + " ".join(want))
    check("--passes" not in duty_flags, f"время работы не обрезается числом проходов: {duty_flags}")
    check(
        "работаю 6 ч" in payload["message"],
        f"панель объясняет, что прогон занят это время: {payload['message']}",
    )
    check(
        webui.load_settings()["repeat_pause_sec"] == 30,
        "пауза на время не переписывает настройки прогона",
    )
    # а без «Работать, ч» всё как раньше: пауза из настроек, проходы из поля
    before = len(spawned)
    code, payload = call(
        "/api/run", "POST", {"hours": 6, "run_hours": 0, "passes": 2, "repeat_pause": 30}
    )
    plain_flags = spawned[before][1] if len(spawned) > before else []
    passes_at = plain_flags.index("--passes") if "--passes" in plain_flags else -1
    check(
        "--run-hours" not in plain_flags and passes_at >= 0 and plain_flags[passes_at + 1] == "2",
        f"без «Работать, ч» команда прежняя (проходы из поля, без --run-hours): {plain_flags}",
    )

    # 6.5. «Войти в VK»: вход идёт для нужного аккаунта
    # Уже есть +79990000001 и LOGIN:PASS:TOKEN → третий аккаунт будет №3.
    code, payload = call("/api/accounts/add", "POST", {"content": "+79990000002;пароль;;"})
    check(
        payload["ok"] and "всего уникальных 3" in payload["message"],
        f"в файле аккаунтов стало три: {payload['message']}",
    )
    code, payload = call("/api/accounts")
    check(all("number" in row for row in payload["rows"]), "у строк аккаунтов есть номер для входа")
    code, payload = call("/api/login", "POST", {"account": 3})
    check(payload["ok"] and spawned[-1][0] == "vk_auth", f"«Войти в VK» запускает вход: {payload.get('message')}")
    check(spawned[-1][1][:3] == ["--login", "--account", "3"], f"вход в третий аккаунт: {spawned[-1][1]}")
    check(
        "--profile" in spawned[-1][1] and spawned[-1][1][-1].endswith("vk_browser_profile_3"),
        f"у третьего аккаунта свой профиль браузера: {spawned[-1][1][-1]}",
    )
    code, payload = call("/api/login", "POST", {"account": 0})
    check(not payload["ok"] and "нет в accounts.txt" in payload["message"], f"чужой номер не пускаем: {payload['message']}")

    # 6.6. «Войти во все, кому нужен вход»: одна кнопка — очередь входов по одному.
    # Аккаунт №4 — случай из жизни (скриншот из панели): файл сессии есть, прокси есть,
    # а токен протух (VK: access_token has expired) — вход нужен заново, и кнопка
    # обязана взять его в очередь, а не отвечать «все уже входили».
    code, payload = call(
        "/api/accounts/add", "POST", {"content": "+79990000003;пароль;;http://u:p@127.0.0.1:1080"}
    )
    check(payload["ok"], f"аккаунт с прокси добавлен: {payload['message']}")
    (tmp / "session_79990000003.json").write_text(
        '{"access_token": "vk1.a.ExpiredToken"}', encoding="utf-8"
    )
    panel_db.mark_account(
        "+79990000003",
        proxy="http://u:p@127.0.0.1:1080",
        status="dead",
        error="users.get: VK API error 5: User authorization failed: access_token has expired.",
    )
    code, data = call("/api/accounts")
    need_login = sorted((str(row["number"]) for row in data["rows"] if row["needs_login"]), key=int)
    alive = {str(row["number"]) for row in data["rows"] if row["session"] and not row["needs_login"]}
    dead_row = next(row for row in data["rows"] if str(row["number"]) == "4")
    check(
        dead_row["session"] and dead_row["status"] == "dead" and dead_row["needs_login"],
        f"мёртвый аккаунт с файлом сессии всё равно просит вход: {dead_row}",
    )
    check(need_login == ["1", "3", "4"], f"очередь входа = без сессии + мёртвые: {need_login}")
    check(bool(alive), f"у аккаунта из TOKEN сессия жива: {sorted(alive)}")
    check(not set(need_login) & alive, "аккаунты с живой сессией в очередь не попадают")
    before = len(spawned)
    # gap=0: в проверке окна никто не закрывает, паузы между входами не нужны
    code, payload = call("/api/login/all", "POST", {"gap": 0})
    check(payload["ok"], f"«Войти во все, кому нужен вход» запускается: {payload.get('message')}")
    check("без сессии" in payload["message"], f"кнопка объясняет, кого берёт: {payload['message']}")
    code, progress = call("/api/progress")
    check(
        progress["login_batch"]["total"] == len(need_login),
        f"в очередь попали без сессии и мёртвые: {progress['login_batch']}",
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        _code, progress = call("/api/progress")
        if not progress["login_batch"]["active"]:
            break
        time.sleep(0.2)
    check(not progress["login_batch"]["active"], "очередь входа доходит до конца")
    check(
        progress["login_batch"]["done"] == len(need_login) and not progress["login_batch"]["failed"],
        f"очередь открыла окно каждому: {progress['login_batch']}",
    )
    forced = [row for row in spawned[before:] if row[0] == "vk_auth"]
    numbers = [str(row[1][2]) for row in forced]
    check(numbers == need_login, f"входы пошли по без сессии и мёртвым, по порядку: {numbers}")
    check(not set(numbers) & alive, "аккаунты с живой сессией очередь пропускает")
    check(all("--profile" in row[1] for row in forced), "у каждого входа свой профиль браузера")

    # 6.7. Все сессии живы — очередь не запускается и объясняет почему
    for login in ("+79990000001", "+79990000002", "+79990000003"):
        panel_db.mark_account(login, status="ready")
        webui._session_path(login).write_text('{"access_token": "vk1.a.Alive"}', encoding="utf-8")
    before = len(spawned)
    code, payload = call("/api/login/all", "POST", {"gap": 0})
    check(
        not payload["ok"] and "нужен вход" in payload["message"],
        f"когда вход никому не нужен, очередь не запускается: {payload['message']}",
    )
    check(len(spawned) == before, "лишние окна входа не открываются")

    # 6.8. Прогон не запустился («код 2»: готовых аккаунтов нет) — панель сама
    # открывает очередь входа, а не ждёт нажатия кнопки. Настройка
    # auto_login_on_fail это умеет выключать.
    class StubRunner:
        """Заглушка процесса: автовходу нужны только log() и start()."""

        def __init__(self) -> None:
            self.logs: list[str] = []
            self.running = False

        def log(self, msg: str) -> None:
            self.logs.append(msg)

        def start(self, script: str, extra: list[str], label: str) -> tuple[bool, str]:
            spawned.append([script, list(extra), label])
            return True, ""

    (tmp / "settings.json").write_text(
        json.dumps({"auto_login_on_fail": True}), encoding="utf-8"
    )
    stub = StubRunner()
    before = len(spawned)
    webui._last_login["t"] = time.time() - 300  # пауза между входами уже вышла
    webui.autologin_after_failure(stub, "проверка", 2)
    check(len(spawned) == before and not stub.logs, "«код 2» у проверки аккаунтов вход не открывает")
    webui.autologin_after_failure(stub, webui.RUN_LABEL, 0)
    check(len(spawned) == before and not stub.logs, "успешный прогон (код 0) автовход не запускает")

    # Сессия №3 протухла (как в жизни: «access_token has expired»), вход нужен ей одной.
    panel_db.mark_account(
        "+79990000002",
        status="dead",
        error="users.get: VK API error 5: User authorization failed: access_token has expired.",
    )
    _code, data = call("/api/accounts")
    need_now = sorted((str(row["number"]) for row in data["rows"] if row["needs_login"]), key=int)
    check(need_now == ["3"], f"протухшая сессия снова просит вход: {need_now}")
    webui.autologin_after_failure(stub, webui.RUN_LABEL, 2)
    check(
        any("автовход: прогон не запустился" in line for line in stub.logs),
        f"в логе объяснено, почему открылся вход: {stub.logs}",
    )
    check(webui._login_batch["active"], "очередь входа пошла сама, без кнопки")
    deadline = time.time() + 20
    while webui._login_batch["active"] and time.time() < deadline:
        time.sleep(0.2)
    forced_now = [row for row in spawned[before:] if row[0] == "vk_auth"]
    check(
        [str(row[1][2]) for row in forced_now] == need_now,
        f"окно открылось аккаунту с протухшим токеном: {forced_now}",
    )

    (tmp / "settings.json").write_text(
        json.dumps({"auto_login_on_fail": False}), encoding="utf-8"
    )
    quiet = StubRunner()
    before = len(spawned)
    webui.autologin_after_failure(quiet, webui.RUN_LABEL, 2)
    check(
        len(spawned) == before and not webui._login_batch["active"],
        "auto_login_on_fail: false — автовход молчит",
    )
    check(any("выключен" in line for line in quiet.logs), f"и говорит, где войти: {quiet.logs}")
    panel_db.mark_account("+79990000002", status="ready")

    # 6.9. Хук висит на самом процессе: выход с кодом 2 поднимает автовход сам
    seen: list[dict] = []
    real_login_all = webui.action_login_all
    webui.action_login_all = lambda runner, body: (seen.append(dict(body)), (True, "очередь входа (заглушка)"))[1]
    (tmp / "settings.json").write_text("{}", encoding="utf-8")  # автовход включён по умолчанию
    live2 = webui.Runner()
    ok, message = live2.spawn([sys.executable, "-c", "raise SystemExit(2)"], webui.RUN_LABEL)
    check(ok, f"процесс «прогон» запустился: {message}")
    deadline = time.time() + 15
    while (live2.running or not seen) and time.time() < deadline:
        time.sleep(0.1)
    check(seen == [{}], f"выход прогона с кодом 2 поднял автовход: {seen}")
    lines_now = [line for _seq, line in live2._lines]
    check(
        any("автовход: прогон не запустился" in line for line in lines_now),
        f"в логе панели видна причина автовхода: {lines_now[-3:]}",
    )
    webui.action_login_all = real_login_all

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
    log_text = live.log_file.path.read_text(encoding="utf-8")
    check("пошёл" in log_text, f"лог прогона сохранён в файл: {live.log_file.path.name}")
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
    webui.LOG_DIR = real_log_dir
    webui.panel_db = panel_db_before
    settings_mod.CONFIG_FILE = real_config
    paths_mod.DATA_DIR = real_data_dir
    accounts_mod.DATA_DIR = real_accounts_data
    shutil.rmtree(tmp, ignore_errors=True)

# 9. настоящие файлы данных не пострадали
check(TAGS_FILE.read_bytes() == tags_before, "data/tags.txt остался как был")
check(ACCOUNTS_FILE.read_bytes() == accounts_before, "data/accounts.txt остался как был")
config_now = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else b""
check(config_now == config_before, "data/settings.json остался как был")
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ПАНЕЛЬ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)
