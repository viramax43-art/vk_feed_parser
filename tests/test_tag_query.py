#!/usr/bin/env python3
"""Чем искать тег в запросе к VK: «с #» (хештег) или «без #» (слово).

    python tests/test_tag_query.py

Сеть, браузер, база и настоящие данные не нужны: настройки живут во временном файле,
прогон и проба останавливаются раньше первого запроса (списка тегов нет), а окно
обхода в тесте не поднимается. Режим тега — одна настройка на весь прогон, поэтому
проверяем всю цепочку:

  1) настройка tag_query: как значение приводится к одному виду («без #», «слово»,
     «word» — слово; непонятное — прежний хештег), как подписано в интерфейсе, что
     по умолчанию хештег, и что `python src/settings.py --tag-query word` пишет слово;
  2) флаг прогона --tag-query и resolve_tag_query: флаг сильнее настройки, а без флага
     режим берётся из data/settings.json;
  3) запрос к VK (vk_client.search_query): «hash» — «#тег» (как было всегда), «word» —
     «тег» без решётки; vk_client.set_tag_query нормализует режим и возвращает его;
  4) прогон целиком (src/run_tags.py): режим ставится ДО первых запросов — общий для
     всех воркеров, и в логе видно «тег в запросе: …»;
  5) проба одного тега (src/probe.py) берёт тот же режим: флаг сильнее настройки;
  6) окно обхода ленты показывает ту же выдачу: feed_search_url открывает #тег или
     слово без решётки, и FeedViewer запоминает режим;
  7) команда запуска (src/gui.py) и панель (src/webui.py): «--tag-query word» доходит до
     процесса, настройка сохраняется, а на странице «Теги» есть переключатель;
  8) старое меню (app.py): пункт настроек знает про tag_query.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import app as app_mod  # noqa: E402
import gui  # noqa: E402
import probe as probe_mod  # noqa: E402
import run_tags  # noqa: E402
import settings as settings_mod  # noqa: E402
import vk_auth  # noqa: E402
import vk_client  # noqa: E402
import webui  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def quiet(fn, *args, **kwargs):
    """Зовём то, что печатает в консоль, — свой вывод теста не засоряем.

    И stdout, и stderr: argparse при опечатке пишет справку в stderr, а она в тесте
    не нужна (проверяем мы сам отказ, а не текст справки).
    """
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def mode_in_args(args: list[str]) -> str:
    """Режим из собранной команды: --tag-query word → "word", без флага → "hash"."""
    return args[args.index("--tag-query") + 1] if "--tag-query" in args else "hash"


tmp = Path(tempfile.mkdtemp(prefix="tag_query_"))
orig_config = settings_mod.CONFIG_FILE
orig_argv = list(sys.argv)
settings_mod.CONFIG_FILE = tmp / "settings.json"

try:
    # 1. Настройка: режим прогона, и он живёт в data/settings.json.
    check(
        settings_mod.TAG_QUERY_LABELS["hash"] == "с # (хештег)"
        and settings_mod.TAG_QUERY_LABELS["word"] == "без # (слово)"
        and settings_mod.TAG_QUERY_LABELS["auto"] == "авто (по тегу)",
        f"режим подписан в интерфейсе как «с # (хештег)» / «без # (слово)» / «авто (по тегу)»: {settings_mod.TAG_QUERY_LABELS}",
    )
    check(
        settings_mod.DEFAULTS["tag_query"] == "hash",
        "по умолчанию ищем хештег — как было до настройки",
    )
    check(
        all(
            settings_mod.normalize_tag_query(value) == "auto"
            for value in ("auto", "АВТО", "авто", "по тегу", "автоматически")
        ),
        "«авто» понимаем и словом: auto / авто / по тегу",
    )
    words = ("word", "words", "text", "слово", "словом", " СЛОВО ", "без #", "Без #", "no", "off", "0", "false")
    check(
        all(settings_mod.normalize_tag_query(value) == "word" for value in words),
        f"«без #» понимаем и словом, и словом из файла настроек: {words}",
    )
    hashes = ("hash", "с #", "хештег", "#тег", "", None, "как-нибудь")
    check(
        all(settings_mod.normalize_tag_query(value) == "hash" for value in hashes),
        f"всё непонятное — прежний хештег (старые настройки работают как раньше): {hashes}",
    )
    check(
        settings_mod.tag_query_value("без # (слово)") == "word"
        and settings_mod.tag_query_value("с # (хештег)") == "hash"
        and settings_mod.tag_query_value("авто (по тегу)") == "auto",
        "подпись из выпадашки превращается в режим",
    )
    check(
        settings_mod.tag_query_value("без #") == "word" and settings_mod.tag_query_value("") == "hash",
        "в выпадашку можно прислать и сам режим: «без #» — слово, пустое — хештег",
    )

    if settings_mod.CONFIG_FILE.exists():
        settings_mod.CONFIG_FILE.unlink()
    check(settings_mod.load_settings()["tag_query"] == "hash", "файла настроек нет — режим по умолчанию")
    settings_mod.CONFIG_FILE.write_text('{"tag_query": "без #"}', encoding="utf-8")
    check(
        settings_mod.load_settings()["tag_query"] == "word",
        "настройку правят руками: «без #» в файле читается как слово",
    )
    settings_mod.CONFIG_FILE.write_text('{"tag_query": "\u043c\u0443\u0441\u043e\u0440"}', encoding="utf-8")
    check(
        settings_mod.load_settings()["tag_query"] == "hash",
        "непонятное значение в файле не срывает прогон: ищем хештег, как раньше",
    )

    sys.argv = ["settings.py", "--tag-query", "word"]
    quiet(settings_mod.main)
    saved = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
    check(
        saved["tag_query"] == "word",
        f"python src/settings.py --tag-query word сохраняет слово: {saved['tag_query']}",
    )
    # 2. Флаг прогона --tag-query: он же и настройка — кто сильнее.
    sys.argv = list(orig_argv)
    check(
        run_tags.parse_args([]).tag_query is None,
        "без флага прогон берёт режим из настроек (флаг по умолчанию не задан)",
    )
    check(
        run_tags.parse_args(["--tag-query", "word"]).tag_query == "word",
        "флаг --tag-query word разбирается",
    )
    try:
        quiet(run_tags.parse_args, ["--tag-query", "словом"])
        bad_flag = False
    except SystemExit:
        bad_flag = True  # argparse с choices не пускает опечатку в прогон
    check(bad_flag, "опечатка в флаге останавливает прогон, а не тихо ищет не то")
    cases = (
        ([], {"tag_query": "word"}, "word", "настройка tag_query: без флага режим из настроек"),
        (["--tag-query", "word"], {"tag_query": "hash"}, "word", "флаг сильнее настройки"),
        (["--tag-query", "hash"], {"tag_query": "word"}, "hash", "флаг сильнее настройки и наоборот"),
        ([], {"tag_query": "без #"}, "word", "настройку понимаем и словами"),
        ([], {}, "hash", "ни флага, ни настройки — прежний хештег"),
        ([], {"tag_query": "мусор"}, "hash", "мусор в настройке — прежний хештег"),
    )
    for argv, file_settings, want, why in cases:
        mode = run_tags.resolve_tag_query(run_tags.parse_args(argv), dict(file_settings))
        check(mode == want, f"{why}: {argv} + {file_settings} → {mode}")

    # 3. Запрос к VK: у VK хештег и слово — это две разные выдачи.
    check(
        vk_client.set_tag_query("hash") == "hash" and vk_client.search_query("маникюр") == "#маникюр",
        f"«с #»: решётку дописываем — {vk_client.search_query('маникюр')}",
    )
    check(
        vk_client.search_query("#маникюр") == "#маникюр",
        "решётку не удваиваем: #маникюр — тот же запрос",
    )
    check(
        vk_client.set_tag_query("без #") == "word" and vk_client.tag_query() == "word",
        f"vk_client.set_tag_query нормализует режим и возвращает его: {vk_client.tag_query()}",
    )
    check(
        vk_client.search_query("маникюр") == "маникюр"
        and vk_client.search_query("#маникюр") == "маникюр",
        f"«без #»: решётку снимаем, ищем слово — {vk_client.search_query('#маникюр')}",
    )
    check(
        vk_client.search_query("  #маникюр  ") == "маникюр",
        "пробелы и решётка в теге приводятся к слову",
    )
    check(
        vk_client.search_query("маникюр", "hash") == "#маникюр",
        "режим можно передать явно — тогда настройка не мешает (нужно тестам и отладке)",
    )
    vk_client.set_tag_query("hash")

    # 4. Прогон целиком: режим один, ставится до первых запросов, и его видно в логе.
    lines: list[str] = []
    orig_log = run_tags.log
    run_tags.log = lines.append  # свой лог: чужой вывод теста не мешает
    empty = tmp / "tags_empty.txt"
    empty.write_text("", encoding="utf-8")
    try:
        settings_mod.CONFIG_FILE.write_text("{}", encoding="utf-8")
        lines.clear()
        code = run_tags.main([str(empty), "--tag-query", "word"])
        check(
            code == 1 and vk_client.tag_query() == "word",
            f"флагом --tag-query word прогон ищет словом и выходит на пустом списке тегов: {code}",
        )
        check(
            any("тег в запросе: без # (слово)" in line for line in lines),
            f"в логе прогона видно выбранный режим: {[l for l in lines if 'тег в запросе' in l]}",
        )
        check(
            any("ищу слово в тексте постов" in line for line in lines),
            "в логе сказано, чем это отличается от #тега (другая выдача)",
        )

        vk_client.set_tag_query("hash")
        settings_mod.CONFIG_FILE.write_text('{"tag_query": "word"}', encoding="utf-8")
        lines.clear()
        run_tags.main([str(empty)])
        check(
            vk_client.tag_query() == "word",
            "без флага прогон берёт режим из data/settings.json (та же настройка, что в панели)",
        )

        vk_client.set_tag_query("word")
        lines.clear()
        run_tags.main([str(empty), "--tag-query", "hash"])
        check(
            vk_client.tag_query() == "hash",
            "флаг прогона перебивает настройку: можно разово собрать хештегом",
        )
    finally:
        run_tags.log = orig_log
        vk_client.set_tag_query("hash")

    # 5. Проба одного тега: режим тот же, что у прогона (до первого запроса к VK).
    probe_logs: list[str] = []
    orig_probe_log = probe_mod.log
    orig_build_client = probe_mod.build_client
    probe_mod.log = probe_logs.append

    def no_session(_args):
        """Дальше теста дело не идёт: клиента нет — проба на этом и останавливается."""
        raise probe_mod.VkAuthError("нет сессии в тесте")

    probe_mod.build_client = no_session
    try:
        settings_mod.CONFIG_FILE.write_text('{"tag_query": "word"}', encoding="utf-8")
        probe_logs.clear()
        code = probe_mod.main(["маникюр"])
        check(
            code == 1 and vk_client.tag_query() == "word",
            f"проба без флага берёт режим из настроек: {code}",
        )
        check(
            any("тег в запросе: без # (слово)" in line for line in probe_logs),
            f"проба пишет выбранный режим в лог: {[l for l in probe_logs if 'тег в запросе' in l]}",
        )
        probe_logs.clear()
        probe_mod.main(["маникюр", "--tag-query", "hash"])
        check(
            vk_client.tag_query() == "hash",
            "у пробы флаг --tag-query сильнее настройки",
        )
    finally:
        probe_mod.log = orig_probe_log
        probe_mod.build_client = orig_build_client
        vk_client.set_tag_query("hash")

    # 6. Окно обхода ленты: обход идёт HTTP-запросами, поэтому окно открывает ту же
    #    выдачу, что берёт прогон — с решёткой или словом.
    check(
        vk_auth.feed_search_url("маникюр")
        == "https://m.vk.ru/search/statuses?q=%23%D0%BC%D0%B0%D0%BD%D0%B8%D0%BA%D1%8E%D1%80",
        f"«с #»: окно открывает поиск по хештегу — {vk_auth.feed_search_url('маникюр')}",
    )
    check(
        vk_auth.feed_search_url("маникюр", "vk.ru", "word")
        == "https://m.vk.ru/search/statuses?q=%D0%BC%D0%B0%D0%BD%D0%B8%D0%BA%D1%8E%D1%80",
        f"«без #»: окно открывает ту же выдачу словом — {vk_auth.feed_search_url('маникюр', 'vk.ru', 'word')}",
    )
    check(
        vk_auth.feed_search_url("маникюр", "vk.ru", "без #")
        == vk_auth.feed_search_url("маникюр", "vk.ru", "word"),
        "режим окна нормализуется так же, как настройка",
    )
    check(
        vk_auth.FeedViewer(tmp / "session_view.json", "", tag_query="без #").tag_query == "word"
        and vk_auth.FeedViewer(tmp / "session_view.json", "").tag_query == "hash",
        "окно запоминает режим: по умолчанию хештег, а «без #» — слово",
    )

    # 7. Команда запуска и панель: настройка доходит до процесса.
    word_args = gui.build_run_args(tag_query="word")
    check(
        mode_in_args(word_args) == "word",
        f"команда прогона несёт --tag-query word: {word_args}",
    )
    check(
        "--tag-query" not in gui.build_run_args(),
        "без выбора режима команда остаётся прежней (как было до настройки)",
    )
    check(
        mode_in_args(gui.build_run_args(tag_query="hash")) == "hash",
        "«с #» окно просит явно — прогон получает ровно то, что выбрано",
    )
    check(
        mode_in_args(gui.build_run_args(tag_query="без #")) == "word",
        "подпись из окна («без # (слово)») превращается в режим",
    )

    spawned: list[list] = []

    class FakeRunner(webui.Runner):
        """Прогон не запускаем: запоминаем команду и делаем вид, что стартовали."""

        def start(self, script: str, extra: list[str], label: str) -> tuple[bool, str]:
            spawned.append([script, list(extra), label])
            return True, ""

    panel_tags = tmp / "tags.txt"
    panel_tags.write_text("маникюр\n", encoding="utf-8")
    settings_mod.CONFIG_FILE.write_text("{}", encoding="utf-8")
    spawned.clear()
    ok, message = webui.action_run(
        FakeRunner(), {"tag_query": "без # (слово)", "tags_file": str(panel_tags)}
    )
    panel_settings = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
    check(
        ok and panel_settings["tag_query"] == "word",
        f"панель сохраняет режим в data/settings.json (tag_query): {panel_settings.get('tag_query')}",
    )
    check(
        spawned and mode_in_args(spawned[0][1]) == "word",
        f"«Старт» уходит в прогон с выбранным режимом: {spawned and spawned[0][1]}",
    )
    check(
        "словом" in message,
        f"панель говорит, что тег ищется словом (видно, что это другая выдача): {message}",
    )
    ok, message = webui.action_save_settings({"tag_query": "с # (хештег)"})
    panel_settings = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
    check(
        ok and panel_settings["tag_query"] == "hash" and "с # (хештег)" in message,
        f"переключатель «с #» возвращает прежний режим: {message}",
    )

    index_html = (ROOT / "src" / "webui" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "src" / "webui" / "app.js").read_text(encoding="utf-8")
    check(
        'id="tag-query"' in index_html
        and '<option value="hash">с # (хештег)</option>' in index_html
        and '<option value="word">без # (слово)</option>' in index_html
        and '<option value="auto">авто (по тегу)</option>' in index_html,
        "на странице «Теги» есть переключатель «с #» / «без #» / «авто»",
    )
    check(
        'tag_query: $("tag-query").value' in app_js,
        "страница отправляет выбранный режим вместе со стартом прогона",
    )
    check(
        'settings.tag_query === "word" || settings.tag_query === "auto"' in app_js,
        "переключатель подставляется из сохранённой настройки tag_query",
    )
    check(
        '$("tag-query").addEventListener("change", () => void saveTagQuery())' in app_js,
        "выбор сохраняется сразу — это настройка прогона, а не галочка запуска",
    )
    # Путь сохранения: страница шлёт выбор тем же методом, которым маршрут объявлен на
    # сервере (settings.json правит PUT /api/settings). Пока страница звала POST, панель
    # отвечала «нет маршрута /api/settings», и режим тега молча не сохранялся.
    webui_src = (ROOT / "src" / "webui.py").read_text(encoding="utf-8")
    save_body = app_js.split("function saveTagQuery()", 1)[1].split("\n}", 1)[0]
    put_routes = webui_src.split("def do_PUT", 1)[1].split("def ", 1)[0]
    post_routes = webui_src.split("def do_POST", 1)[1].split("def do_PUT", 1)[0]
    check(
        'putJson("/api/settings"' in save_body,
        "«с #» / «без #» сохраняется через PUT — тем же методом, что знает сервер",
    )
    check(
        '"/api/settings"' in put_routes and '"/api/settings"' in post_routes,
        "на сервере есть и PUT, и POST /api/settings (открытая страница из кеша тоже сохранит)",
    )

    # 8. Старое меню (app.py): пункт настроек знает про tag_query.
    saved_settings: list[dict] = []
    orig_ask = app_mod.ask
    orig_menu_load = app_mod.load_settings
    orig_menu_save = app_mod.save_settings
    app_mod.load_settings = lambda: dict(settings_mod.DEFAULTS)
    app_mod.save_settings = saved_settings.append
    app_mod.ask = lambda prompt, default="": "без #" if "чем искать тег" in prompt else ""
    try:
        quiet(app_mod.settings_menu)
    finally:
        app_mod.ask = orig_ask
        app_mod.load_settings = orig_menu_load
        app_mod.save_settings = orig_menu_save
    check(
        saved_settings and saved_settings[0]["tag_query"] == "без #",
        f"меню app.py даёт задать режим текстом: {saved_settings and saved_settings[0].get('tag_query')}",
    )
    check(
        all(settings_mod.normalize_tag_query(value) == "word" for value in ("без #", "word")),
        "что бы ни вписали в меню, прогон поймёт это как слово",
    )
finally:
    settings_mod.CONFIG_FILE = orig_config
    sys.argv = list(orig_argv)
    vk_client.set_tag_query("hash")
    shutil.rmtree(tmp, ignore_errors=True)

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "РЕЖИМ ТЕГА: «с #» ИЛИ «БЕЗ #» — НА ВЕСЬ ПРОГОН")
raise SystemExit(1 if FAILED else 0)
