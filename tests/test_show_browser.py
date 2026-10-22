#!/usr/bin/env python3
"""Показ окна браузера-эмулятора, пока прогон собирает ленту.

    python tests/test_show_browser.py

Сеть, браузер и база не нужны: vk_auth подменяется заглушками, настройки живут во
временном файле. Проверяем то, из-за чего «работу эмулятора» не было видно:

  1) настройка show_browser: по умолчанию окно скрыто, «on» в файле настроек
     понимается, а `python src/settings.py --show-browser on|off` включает и
     выключает показ;
  2) флаг --show-browser у прогона: parse_args его знает, resolve_show_browser
     поднимает показ из настроек и гасит его, когда браузер выключен совсем
     (--no-browser) или показать нечем (нет графической сессии);
  3) окно действительно открывается: обновление токена (профиль ленты + F5), F5 при
     пустой выдаче и фон обновления сессии зовут vk_auth с headless=False;
  4) без показа путь прежний: vk_auth зовут БЕЗ лишнего параметра — старые вызовы и
     заглушки продолжают работать;
  5) воркер прогона передаёт «показывать» в F5: до места доходят и флаг, и настройка;
  6) окно обхода ленты: обход идёт HTTP-запросами (newsfeed.search), поэтому окно
     открывает ту же выдачу по тегу («с #» — хештег, «без #» — слово, как в настройке
     tag_query) и подписывает её — какое окно времени берётся;
     браузер при этом один на прогон, окна времени обход не задерживают, а сбой окна
     обход не срывает. F5 окно не жмёт вообще: страница отдаёт посты только через
     несколько секунд (проверено живым браузером), и перезагрузка снова оставляла бы
     окно пустым — окна времени сменяются прокруткой (в подписи видно посты в ленте),
     а выдача открывается заново только там, где листать больше нечего: сменился тег
     или прокрутить не вышло. Открывается окно мобильным экраном (390×844, тач,
     телефонный user-agent — как лента в телефоне) и листает её неровным шагом каждые
     0.2–0.45 с: не только на окнах обхода, но и между ними, пока прогон обходит окно
     времени. Cookies сессии окну ставятся доменом
     (.vk.ru/.vk.com) — иначе VK видит только сам vk.ru, а поддомены (login.vk.ru,
     api.vk.ru) остаются анонимными: окно получает «ошибку сети», а после F5 —
     страницу входа. Если VK всё-таки показал вход, окно это замечает, говорит в лог
     и перечитывает cookies (страницу входа оно не листает); саму выдачу окно грузит,
     а чужую рекламу и счётчики — нет (в консоли окна они оставляют 403/504/CORS, а
     постам, которые VK отдаёт не сразу, отнимают полосу); шум загрузчика статики VK
     («Not found blocking … asset from window_proxy.js / performance_observers.js»)
     окно в консоли глушит (и окно обхода, и окно F5) — это предупреждение самого VK
     о теге на другом CDN-зеркале, а не ошибка окна;
  7) команда запуска и панель: build_run_args(show_browser=True) даёт --show-browser,
     галочка есть и в окне, и на странице панели.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import gui  # noqa: E402
import run_tags  # noqa: E402
import settings as settings_mod  # noqa: E402
import vk_auth  # noqa: E402
from account_pool import READY  # noqa: E402
from state_db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


class FakeClient:
    """Клиент аккаунта: путям обновления нужны только токен и версия API."""

    def __init__(self, login: str) -> None:
        self.login = login
        self.token = "vk1.a.живой"
        self.api_version = "5.289"


class FakePool:
    """Пул-заглушка: воркеру нужен add_posts, окну обхода — список аккаунтов."""

    def __init__(self, accounts=None) -> None:
        self.posts = 0
        self.accounts = list(accounts or [])

    def add_posts(self, acc, count) -> None:
        self.posts += int(count or 0)

    def items(self):
        return list(self.accounts)


class FakeViewer:
    """Окно обхода — заглушка: настоящий браузер в тесте не нужен.

    Пишем, что просили показать (тег и подпись) и сколько раз, а `made` помнит все
    созданные окна: по нему видно, что браузер на прогон один.
    """

    made: list["FakeViewer"] = []

    def __init__(
        self,
        session_path,
        proxy_raw="",
        label="",
        *,
        timeout_ms=0,
        tag_query="hash",
    ) -> None:
        self.session_path = str(session_path)
        self.proxy = proxy_raw
        self.label = label
        self.timeout_ms = timeout_ms
        # Чем окно ищет тег: «hash» — #тег, «word» — словом без решётки. Так видно,
        # что окно получает тот же режим, что и запросы прогона (tag_query).
        self.tag_query = tag_query
        self.visits: list[tuple[str, str]] = []
        self.started = False
        self.stopped = False
        self.alive_now = True
        FakeViewer.made.append(self)

    def start(self) -> "FakeViewer":
        self.started = True
        return self

    def visit(self, tag, caption="", session_path=None) -> None:
        self.visits.append((tag, caption))

    def stop(self, timeout=0.0) -> None:
        self.stopped = True
        self.alive_now = False

    def alive(self) -> bool:
        return self.alive_now


tmp = Path(tempfile.mkdtemp(prefix="show_browser_"))
orig_config = settings_mod.CONFIG_FILE
orig_display = run_tags.has_display
orig_argv = list(sys.argv)
orig_viewer = vk_auth.FeedViewer  # настоящее окно: в тесте его подменяем заглушкой
calls: list[dict] = []  # с чем звали vk_auth: видно, передан ли headless
logs: list[str] = []  # строки лога прогона (свой лог — чтобы чужой вывод не мешал)
db: DB | None = None


def stub_via_browser(session_path, proxy_raw="", timeout=90, **kw):
    """Обновление токена через профиль браузера (F5) — без браузера."""
    calls.append({"fn": "refresh_via_browser", "headless": kw.get("headless", "нет параметра")})
    return {"access_token": "vk1.a.видимый", "api_version": "5.289"}


def stub_feed_reload(session_path, proxy_raw="", timeout=90, **kw):
    """Заход в ленту профилем и F5 — без браузера."""
    calls.append({"fn": "token_via_feed_reload", "headless": kw.get("headless", "нет параметра")})
    return {"access_token": "vk1.a.видимый", "api_version": "5.289"}


try:
    # 1. Настройка show_browser: по умолчанию скрыто, CLI включает и выключает.
    settings_mod.CONFIG_FILE = tmp / "settings.json"
    check(
        settings_mod.load_settings()["show_browser"] is False,
        "без файла настроек окно браузера скрыто (show_browser = false)",
    )
    settings_mod.save_settings({"show_browser": True})
    check(
        settings_mod.load_settings()["show_browser"] is True,
        "show_browser: true из файла настроек читается",
    )
    settings_mod.CONFIG_FILE.write_text(json.dumps({"show_browser": "on"}), encoding="utf-8")
    check(
        settings_mod.load_settings()["show_browser"] is True,
        "файл правят руками: «on» в show_browser тоже включаёт показ",
    )

    def run_settings_cli(*argv: str) -> None:
        sys.argv = ["settings.py", *argv]
        with contextlib.redirect_stdout(io.StringIO()):
            settings_mod.main()

    run_settings_cli("--show-browser", "off")
    check(
        settings_mod.load_settings()["show_browser"] is False,
        "`settings.py --show-browser off` выключает показ окна",
    )
    run_settings_cli("--show-browser", "on")
    check(
        settings_mod.load_settings()["show_browser"] is True,
        "`settings.py --show-browser on` включает показ окна",
    )
    sys.argv = list(orig_argv)

    # 2. Флаг прогона и правила: что важнее, что мешает.
    check(run_tags.parse_args([]).show_browser is False, "--show-browser у прогона по умолчанию выключен")
    check(run_tags.parse_args(["--show-browser"]).show_browser is True, "флаг --show-browser разбирается")
    show, why = run_tags.resolve_show_browser(
        run_tags.parse_args(["--show-browser"]), {"show_browser": False}
    )
    check(show and not why, f"флаг показывает окно даже против настройки: {show}/{why!r}")
    show, why = run_tags.resolve_show_browser(run_tags.parse_args([]), {"show_browser": True})
    check(show and not why, f"настройки show_browser хватает без флага: {show}/{why!r}")
    show, why = run_tags.resolve_show_browser(run_tags.parse_args([]), {"show_browser": False})
    check(not show and not why, "никто не просил — окно скрыто и в лог ничего не пишем")
    show, why = run_tags.resolve_show_browser(
        run_tags.parse_args(["--show-browser", "--no-browser"]), {}
    )
    check(
        not show and "--no-browser" in why,
        f"с --no-browser показывать нечего (браузер выключен совсем): {why!r}",
    )
    # 3-4. Окно открывается там, где эмулятор трогает ленту, и только когда просили.
    vk_auth.refresh_via_browser = stub_via_browser
    vk_auth.token_via_feed_reload = stub_feed_reload
    client = FakeClient("79990000001")
    session = tmp / "session_79990000001.json"

    calls.clear()
    ok = run_tags._refresh_client(client, session, "http://u:p@1.1.1.1:80", "79990000001", show=True)
    check(
        ok and calls[-1]["headless"] is False,
        f"обновление токена идёт в видимом окне (headless=False): {calls}",
    )
    check(client.token == "vk1.a.видимый", "свежий токен всё так же подставляется клиенту")

    calls.clear()
    run_tags._refresh_client(client, session, "", "79990000001")
    check(
        calls[-1]["headless"] == "нет параметра",
        f"без показа путь прежний — лишний параметр не передаём: {calls}",
    )

    calls.clear()
    ok = run_tags._f5_refresh(client, session, "", "79990000001", show=True)
    check(
        ok and calls[-1]["fn"] == "token_via_feed_reload" and calls[-1]["headless"] is False,
        f"F5 при пустой выдаче показывается (лента на виду): {calls}",
    )

    calls.clear()
    run_tags._f5_refresh(client, session, "", "79990000001")
    check(
        calls[-1]["headless"] == "нет параметра",
        f"скрытый F5 работает как раньше: {calls}",
    )
    run_tags._browser_prefer_until.clear()

    # Фон обновления сессии: окно тоже на виду, когда показ включён.
    from account_pool import AccountPool, PoolAccount

    pool_login = "79990000002"
    pool_session = tmp / f"session_{pool_login}.json"
    pool_session.write_text("{}", encoding="utf-8")
    pool = AccountPool([PoolAccount(login=pool_login, proxy="", session_path=pool_session)])
    refresher = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 15}, show_browser=True)
    calls.clear()
    payload = refresher._via_browser(refresher.accounts()[0])
    check(
        payload.get("access_token") == "vk1.a.видимый" and calls[-1]["headless"] is False,
        f"фон обновления сессии открывает окно на виду: {calls}",
    )
    quiet = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 15})
    calls.clear()
    quiet._via_browser(quiet.accounts()[0])
    check(
        calls[-1]["headless"] == "нет параметра",
        f"без показа фон обновляет сессию скрыто: {calls}",
    )

    run_tags.has_display = lambda: False
    try:
        show, why = run_tags.resolve_show_browser(run_tags.parse_args(["--show-browser"]), {})
    finally:
        run_tags.has_display = orig_display
    check(
        not show and "графической сессии" in why,
        f"на сервере без экрана окно не показываем — только строка в логе: {why!r}",
    )

    # 5. Воркер прогона: «показывать» доходит до F5 и с флагом, и из настроек.
    run_tags.has_display = lambda: True  # экран «есть»: проверяем сам флаг, не машину
    run_tags.silent_reset()
    run_tags.log = logs.append
    # Окно обхода — заглушка: настоящий браузер в тесте не открываем.
    vk_auth.FeedViewer = FakeViewer
    FakeViewer.made.clear()
    run_tags._viewer = None
    orig_window = run_tags.probe_mod.run_window
    orig_throttle = run_tags.empty_is_throttle
    orig_export = run_tags.maybe_live_export
    orig_f5 = run_tags._f5_refresh

    def fake_window(client, _db, tag, **kw):
        """VK молча отдаёт пустую выдачу — значит, воркер дойдёт до F5."""
        return {"status": "ok", "reason": "end_of_feed", "seen": 0, "new": 0, "pages": 1,
                "need_split": False, "cursor": ""}

    f5_shows: list[bool] = []

    def fake_f5(client, session_path, proxy, label, show=False):
        """Заглушка F5: пишем, показывали ли окно (и дальше не помогаем)."""
        f5_shows.append(bool(show))
        return False

    run_tags.probe_mod.run_window = fake_window
    run_tags.empty_is_throttle = lambda *_a, **_kw: True
    run_tags.maybe_live_export = lambda *_a, **_kw: None
    run_tags._f5_refresh = fake_f5

    def run_empty_window(args, show_setting: bool):
        """Прогоняет одно пустое окно воркером и возвращает, показывали ли F5.

        Аккаунт — свежий на каждый заход: бюджет F5 («одна попытка на аккаунт за
        прогон», empty_state) живёт на самом аккаунте и между вызовами не сбрасывается.
        """
        args.dry_run = False
        args.no_export = True
        args.tag_filter = ""
        args.start_time = 0
        args.end_time = 0
        args.count = 10  # меньше EMPTY_RETRY_COUNT: смену count в этом тесте не проверяем
        settings = dict(settings_mod.load_settings(), show_browser=show_setting)
        acc = type("Acc", (), {
            "login": "79990000003",
            "proxy": "",
            "session_path": tmp / "session_79990000003.json",
            "cooldown_until": 0.0,
            "status": READY,
        })()
        queue = run_tags.JobQueue()
        start = 1_700_000_000
        queue.put(db.add_job("маникюр", start, start + 600, 1))  # < EMPTY_SPLIT_MIN_SEC
        before = len(f5_shows)
        shown_before = sum(len(v.visits) for v in FakeViewer.made)
        outcome = run_tags.work_account(
            FakeClient(acc.login), acc, queue, args, settings, db, FakePool([acc])
        )
        shown = sum(len(v.visits) for v in FakeViewer.made) - shown_before
        return outcome, f5_shows[before:], shown

    db = DB(tmp / "parser.sqlite3")
    outcome, shows, shown = run_empty_window(run_tags.parse_args(["--show-browser"]), False)
    check(
        shows == [True],
        f"флаг --show-browser доходит до F5 в воркере: {shows} (исход {outcome[0]}/{outcome[1]})",
    )
    check(shown >= 1, f"с флагом воркер показывает окно обхода ({shown} показ.)")
    outcome, shows, shown = run_empty_window(run_tags.parse_args([]), True)
    check(shows == [True], f"настройка show_browser тоже доходит до воркера: {shows}")
    check(shown >= 1, f"настройки show_browser хватает и для окна обхода ({shown} показ.)")
    outcome, shows, shown = run_empty_window(run_tags.parse_args([]), False)
    check(shows == [False], f"без показа F5 вызывается скрыто, как раньше: {shows}")
    check(shown == 0, f"без показа окно обхода не зовут ({shown} показ.)")
    outcome, shows, shown = run_empty_window(run_tags.parse_args(["--show-browser", "--no-browser"]), True)
    check(
        shows == [] and outcome[0] == "empty",
        f"с --no-browser воркер вообще не жмёт F5: {shows} ({outcome[0]}/{outcome[1]})",
    )
    check(shown == 0, f"с --no-browser окно обхода тоже молчит ({shown} показ.)")

    # Окно обхода: воркер зовёт его на каждом окне времени — так видно, какое окно
    # обхода берётся. Браузер при этом один на прогон, а не по окну.
    views = [v for v in FakeViewer.made if v.visits]
    check(
        len(FakeViewer.made) == 1 and len(views) == 1 and len(views[0].visits) >= 1,
        "окно обхода на прогон одно (браузер не открывается на каждое окно): "
        f"{[(v.label, len(v.visits)) for v in FakeViewer.made]}",
    )
    first = views[0].visits[0] if views else ("", "")
    check(
        first[0] == "маникюр" and "#маникюр" in first[1]
        and run_tags.fmt_ts(1_700_000_000) in first[1] and "окно #" in first[1],
        f"в окне видно, какое окно времени обходят: {first!r}",
    )
    check(
        views and views[0].label == "79990000003",
        f"окно заходит аккаунтом из пула: {views and views[0].label!r}",
    )

    run_tags.probe_mod.run_window = orig_window
    run_tags.empty_is_throttle = orig_throttle
    run_tags.maybe_live_export = orig_export
    run_tags._f5_refresh = orig_f5

    # 6. Окно обхода ленты само по себе: обход идёт HTTP-запросами, и окно открывает
    #    ту же выдачу по тегу, что берёт прогон.
    check(
        vk_auth.feed_search_url("маникюр")
        == "https://m.vk.ru/search/statuses?q=%23%D0%BC%D0%B0%D0%BD%D0%B8%D0%BA%D1%8E%D1%80",
        f"ссылка окна — поиск по хештегу: {vk_auth.feed_search_url('маникюр')}",
    )
    check(
        vk_auth.feed_search_url("#маникюр", "vk.ru", "word")
        == "https://m.vk.ru/search/statuses?q=%D0%BC%D0%B0%D0%BD%D0%B8%D0%BA%D1%8E%D1%80",
        "«без #» окно открывает словом, без решётки: "
        f"{vk_auth.feed_search_url('маникюр', 'vk.ru', 'word')}",
    )
    check(
        vk_auth.feed_search_url("маникюр", "vk.ru", "хештег")
        == vk_auth.feed_search_url("маникюр"),
        "непонятный режим в окне — прежняя выдача (с решёткой), как в настройках",
    )
    check(
        vk_auth.feed_search_url("#маникюр", "vk.com") == vk_auth.feed_search_url("маникюр").replace(
            "vk.ru", "vk.com"
        ),
        "решётку в теге не удваиваем, а vk.com — тот же путь (как в FEED_RELOAD_URLS)",
    )
    check(
        vk_auth.feed_search_url("") == "https://m.vk.ru/search/statuses",
        f"пустой тег не рождает запрос из одной решётки: {vk_auth.feed_search_url('')}",
    )
    check(
        vk_auth.feed_search_url("  #маникюр  ") == vk_auth.feed_search_url("маникюр"),
        "пробелы и решётку в теге приводим к одному виду",
    )

    # Что окно делает со страницей: открывает поиск по хештегу и ставит подпись.
    # Страница — заглушка (браузер в тесте не нужен), но зовём настоящие _render/_overlay.
    class FakePage:
        """Страница окна: помним адреса, прокрутки и скрипт подписи.

        lands_on — куда VK увёл окно после перехода (вход, пустая страница): по
        этому окно понимает, что лента не открылась. feed — что отвечает лента на
        прокрутки: так видно, догружаются ли посты и где она кончается.
        scroll_fails — сколько первых прокруток падает (страница уехала/закрылась):
        по этому видно, что окно открывает выдачу заново, а не молчит.
        """

        def __init__(self, fails: int = 0, lands_on: str = "", feed=None,
                     scroll_fails: int = 0) -> None:
            self.fails = fails
            self.lands_on = lands_on
            self.feed = list(feed or [])
            self.scroll_fails = scroll_fails
            self.urls: list[str] = []
            self.js: list[tuple[str, str]] = []
            self.scrolls: list[float] = []
            self.url = "about:blank"

        def goto(self, url, wait_until=None, timeout=None):
            if self.fails:
                self.fails -= 1
                raise RuntimeError("страница не открылась")
            self.urls.append(url)
            self.url = self.lands_on or url

        def evaluate(self, script, arg):
            """Прокрутка отвечает как лента: сколько постов, где конец страницы."""
            if script == vk_auth.VIEW_SCROLL_JS:
                if self.scroll_fails:
                    self.scroll_fails -= 1
                    raise RuntimeError("страница закрылась")
                self.scrolls.append(arg)
                if self.feed:
                    return self.feed.pop(0)
                return {"step": 765, "y": 765, "h": 12000, "end": False, "posts": 8}
            self.js.append((script, arg))

    page = FakePage()
    painter = orig_viewer(tmp / "session_paint.json", "")
    painter._page = page
    painter._render("маникюр", "подпись окна")
    check(
        page.urls == [vk_auth.feed_search_url("маникюр")],
        f"окно открывает ту же выдачу по хештегу, что берёт обход: {page.urls}",
    )
    check(
        page.js == [(vk_auth.VIEW_OVERLAY_JS, "подпись окна")],
        f"поверх страницы — подпись прогона тем же скриптом: {page.js}",
    )
    check(
        all(word in vk_auth.VIEW_OVERLAY_JS for word in
            (vk_auth.VIEW_OVERLAY_ID, "getElementById", "textContent", "appendChild", "position:fixed")),
        "подпись ставится поверх страницы и обновляется тем же элементом",
    )
    page_no_caption = FakePage()
    painter._page = page_no_caption
    painter._overlay("")
    check(not page_no_caption.js, f"без подписи окно страницу не трогает: {page_no_caption.js}")
    page_fallback = FakePage(fails=1)
    painter._page = page_fallback
    painter._render("маникюр", "подпись окна")
    check(
        page_fallback.urls == [vk_auth.feed_search_url("маникюр", "vk.com")],
        f"vk.ru не открылся — окно пробует vk.com, как сам обход: {page_fallback.urls}",
    )
    painter._page = FakePage(fails=2)
    try:
        painter._render("маникюр", "подпись окна")
    except vk_auth.VkAuthError as exc:
        check("страница не открылась" in str(exc), f"обе страницы не открылись — сбой окна виден: {exc}")
    else:
        check(False, "обе страницы не открылись, а сбоя окна не заметили")
    painter._page = FakePage()
    check(
        painter._render("маникюр", "подпись окна") is True,
        "лента открылась — окно считает это показом окна обхода",
    )

    # Смену окон времени окно показывает ПРОКРУТКОЙ ленты и НИКОГДА не жмёт F5:
    # страница приходит за секунду, а посты в ней — только через несколько секунд
    # (проверено живым браузером), так что перезагрузка снова оставляла окно пустым.
    # Страница живёт весь прогон, а открывается заново только там, где листать больше
    # нечего: сменился тег или прокрутить не вышло (см. FeedViewer._show).
    vlogs: list[str] = []
    orig_vk_log = vk_auth.log
    vk_auth.log = vlogs.append
    check(
        all(word in vk_auth.VIEW_SCROLL_JS
            for word in ("scrollBy", "visualViewport", "innerHeight", "behavior:", "[data-post-id]")),
        "прокрутка листает выдачу и заодно смотрит, сколько в ней постов",
    )
    live = FakePage()
    painter._page = live
    painter._render("маникюр", "окно 1")
    shown_before = painter.shown
    painter._show("маникюр", "окно 2")
    check(
        live.urls == [vk_auth.feed_search_url("маникюр")]
        and live.scrolls == [vk_auth.VIEW_SCROLL_SHARE]
        and painter.scrolls == 1 and painter.shown == shown_before + 1,
        f"следующее окно времени показывается прокруткой, страницу не перезагружаем: "
        f"{live.urls} (прокруток {live.scrolls}, показов {painter.shown})",
    )
    check(
        live.js[-1] == (vk_auth.VIEW_OVERLAY_JS,
                        "окно 2" + vk_auth.VIEW_POSTS_NOTE.format(n=8)),
        f"на прокрутке подпись обновляется и показывает посты в ленте: {live.js[-1]}",
    )

    # Пауза между прокрутками — 0.2–0.45 с и каждый раз разная: человек листает
    # неровно, а ровный шаг по таймеру выдал бы робота (см. FeedViewer._scroll_pause).
    pauses = [painter._scroll_pause() for _ in range(50)]
    check(
        (vk_auth.VIEW_SCROLL_MIN_SEC, vk_auth.VIEW_SCROLL_MAX_SEC) == (0.2, 0.45)
        and all(
            vk_auth.VIEW_SCROLL_MIN_SEC <= pause <= vk_auth.VIEW_SCROLL_MAX_SEC
            for pause in pauses
        )
        and len(set(pauses)) > 1,
        f"пауза между прокрутками — 0.2–0.45 с и каждый раз разная: {pauses[:3]}",
    )

    # Сколько бы окон времени ни прошло, страница остаётся одна: F5 окно не жмёт —
    # ни «перезагрузить для свежих постов», ни «лента кончилась».
    vlogs.clear()
    for i in range(20):
        painter._show("маникюр", f"окно {i + 3}")
    check(
        len(live.urls) == 1 and len(live.scrolls) == 21 and not vlogs,
        f"страницу окно не перезагружает вообще: переходов {len(live.urls)}, "
        f"прокруток {len(live.scrolls)}, лог {vlogs!r}",
    )

    # Лента кончилась (прокрутка у конца, постов не прибавляется) — и это не повод
    # жать F5: окно времени всё равно показывается прокруткой, а дальше листать уже
    # нечего — эту выдачу прогон прошёл целиком.
    ended = FakePage(feed=[{"h": 12000, "end": True, "posts": 8}] * 3)
    painter._page = ended
    painter._render("маникюр", "конец ленты")
    for _ in range(3):
        painter._show("маникюр", "у конца ленты")
    check(
        len(ended.urls) == 1 and len(ended.scrolls) == 3 and not vlogs,
        f"у конца ленты окно листает дальше, а выдачу не перезагружает: переходов "
        f"{len(ended.urls)}, прокруток {len(ended.scrolls)}, лог {vlogs!r}",
    )

    # Пустая выдача (по тегу ничего нет) — тоже не повод её перезагружать: листаем и
    # ждём, а постов в ленте видно в подписи ровно ноль.
    empty = FakePage(feed=[{"h": 1320, "end": True, "posts": 0}] * 3)
    painter._page = empty
    painter._render("маникюр", "пустая лента")
    for i in range(3):
        painter._show("маникюр", f"пусто {i}")
    check(
        len(empty.urls) == 1 and len(empty.scrolls) == 3,
        f"пустую выдачу окно не перезагружает — листает и ждёт посты: "
        f"{empty.urls} (прокруток {len(empty.scrolls)})",
    )
    check(
        empty.js[-1] == (vk_auth.VIEW_OVERLAY_JS,
                         "пусто 2" + vk_auth.VIEW_POSTS_NOTE.format(n=0)),
        f"пустая лента видна в подписи числом постов: {empty.js[-1]}",
    )

    # Другая выдача (сменился тег) — вот тут страницу и открывают заново: листать
    # чужую выдачу нельзя, в окне должен быть тот тег, что берёт прогон.
    another = FakePage()
    painter._page = another
    shown_before = painter.shown
    painter._show("ресницы", "другой тег")
    check(
        another.urls == [vk_auth.feed_search_url("ресницы")] and not another.scrolls
        and painter.shown == shown_before + 1,
        f"сменился тег — окно открывает именно его выдачу: {another.urls} "
        f"(прокруток {len(another.scrolls)})",
    )

    # Прокрутить не вышло (страница уехала или закрылась) — окно открывает выдачу
    # заново: без этого показывать было бы нечего.
    broken = FakePage(scroll_fails=1)
    painter._page = broken
    vlogs.clear()
    shown_before = painter.shown
    painter._show("ресницы", "лента уехала")
    check(
        broken.urls == [vk_auth.feed_search_url("ресницы")]
        and painter.shown == shown_before + 1
        and any("прокрутить выдачу не вышло" in line for line in vlogs),
        f"прокрутка не вышла — окно открывает выдачу заново: {broken.urls}, лог {vlogs!r}",
    )

    # VK показал вход: листать страницу входа нечего — окно ждёт свежие cookies.
    logins_before = painter.logins
    login_page = FakePage(lands_on="https://login.vk.ru/?act=login")
    painter._page = login_page
    shown_before = painter.shown
    painter._show("маникюр", "вход вместо ленты")
    check(
        login_page.urls == [vk_auth.feed_search_url("маникюр")]
        and not login_page.scrolls and painter.shown == shown_before
        and painter.logins == logins_before + 1,
        f"VK просит вход — страницу входа окно не листает: {login_page.urls} "
        f"(прокруток {len(login_page.scrolls)}, показов {painter.shown})",
    )

    # Cookies сессии окну ставим доменом ("vk.ru" + поддомены login.vk.ru/api.vk.ru),
    # а не host-only по url: иначе VK видит окно анонимным, показывает «ошибку сети»,
    # а после F5 уводит на страницу входа (проверено живым браузером на сессии).
    jar = vk_auth.view_cookie_jar({"remixsid": "abc", "p": "xyz", "": "мимо"})
    check(
        len(jar) == 4
        and {item["domain"] for item in jar} == set(vk_auth.VIEW_COOKIE_DOMAINS)
        and all(item["path"] == "/" and "url" not in item for item in jar),
        f"cookies окна идут на домен .vk.ru/.vk.com: {jar[:1]}",
    )
    check(
        {item["name"] for item in jar} == {"remixsid", "p"},
        "cookies без имени/значения в браузер не подставляем",
    )
    check(
        vk_auth.is_login_page_url("https://login.vk.ru/?act=login")
        and vk_auth.is_login_page_url("https://vk.com/login?act=login")
        and not vk_auth.is_login_page_url(vk_auth.feed_search_url("маникюр")),
        "страницу входа окно отличает от самой выдачи по хештегу",
    )

    # Реклама и чужие счётчики: в ленте они не нужны, а в консоли окна оставляют
    # 403/504/CORS и отнимают полосу у постов, которые VK отдаёт не сразу. Саму
    # выдачу и запросы VK окно грузит как раньше.
    check(
        vk_auth.view_noise_url("https://ad.mail.ru/adq/?callback=1&vk_id=1")
        and vk_auth.view_noise_url("https://stats.vk-portal.net/collect")
        and vk_auth.view_noise_url("https://akashi.vk-portal.net/api/v1/web")
        and vk_auth.view_noise_url("https://R3-TEST.VK.RU/api")
        and vk_auth.view_noise_url("https://user:pass@mincifry-cert.vk.ru:443/check?x=1"),
        "рекламу и чужую статистику окно не грузит "
        "(ad.mail.ru, stats.vk-portal.net, akashi.vk-portal.net)",
    )
    check(
        not vk_auth.view_noise_url(vk_auth.feed_search_url("маникюр"))
        and not vk_auth.view_noise_url("https://api.vk.ru/method/newsfeed.search")
        and not vk_auth.view_noise_url("https://ad.mail.ru.чужой-домен.example/adq/")
        and not vk_auth.view_noise_url(""),
        "саму выдачу и запросы VK окно грузит как раньше (чужое имя под запрет не попадает)",
    )

    class FakeRoute:
        """Запрос браузера: помним, что с ним сделали — заглушили или пустили."""

        def __init__(self, url: str) -> None:
            self.request = type("Req", (), {"url": url})()
            self.done = ""

        def abort(self) -> None:
            self.done = "abort"

        def continue_(self) -> None:
            self.done = "continue"

    class OpenContext:
        """Контекст окна при открытии: помним, что окно поставило браузеру."""

        def __init__(self) -> None:
            self.pages: list = []
            self.routes: list = []
            self.scripts: list = []
            self.timeout = 0
            self.context_args: dict = {}

        def set_default_timeout(self, timeout_ms) -> None:
            self.timeout = timeout_ms

        def add_init_script(self, script) -> None:
            self.scripts.append(script)

        def route(self, url, handler) -> None:
            self.routes.append((url, handler))

        def new_page(self):
            self.pages.append(object())
            return self.pages[-1]

    class OpenBrowser:
        def __init__(self, context: "OpenContext") -> None:
            self.context = context

        def new_context(self, **kw):
            self.context.context_args = dict(kw)
            return self.context

    class OpenChromium:
        """Playwright в тесте: браузер «стоит», запускается и отдаёт свой контекст."""

        def __init__(self, context: "OpenContext") -> None:
            self.context = context

        def launch(self, **kw):
            return OpenBrowser(self.context)

    # Запрет окно ставит браузеру при открытии (в тесте браузер — заглушка): _open
    # вешает перехват на правило view_noise_url, а _route_noise по нему глушит чужой
    # запрос и пропускает свой.
    open_ctx = OpenContext()
    open_pw = type("OpenPw", (), {"chromium": OpenChromium(open_ctx)})()
    orig_vk_display, orig_attempts = vk_auth.has_display, vk_auth.browser_launch_attempts
    vk_auth.has_display = lambda: True  # экран «есть»: проверяем перехват, не машину
    vk_auth.browser_launch_attempts = lambda: [{}]  # браузер «стоит»
    opener = orig_viewer(tmp / "session_open.json", "")
    try:
        opener._open(open_pw)
    finally:
        vk_auth.has_display = orig_vk_display
        vk_auth.browser_launch_attempts = orig_attempts
    check(
        len(open_ctx.routes) == 1 and open_ctx.routes[0][0] is vk_auth.view_noise_url
        and opener._page is open_ctx.pages[0],
        f"окно ставит браузеру запрет на рекламу и статистику: {open_ctx.routes}",
    )
    noise = FakeRoute("https://ad.mail.ru/adq/?vk_id=1")
    feed = FakeRoute(vk_auth.feed_search_url("маникюр"))
    for query in (noise, feed):
        open_ctx.routes[0][1](query)
    check(
        noise.done == "abort" and feed.done == "continue",
        f"чужой запрос окно глушит, а выдачу VK пускает: {noise.done}/{feed.done}",
    )

    # Загрузчик статики VK пишет в консоль «Not found blocking … asset from …»: тег
    # ищется по префиксу window.vk.static.domain + путь, а страница пришла с другого
    # CDN-зеркала. Это шум самого VK (файлы живы, лента работает, от UA не зависит),
    # поэтому окно глушит ровно эту строку и не трогает остальные сообщения.
    check(
        open_ctx.scripts == [vk_auth.VIEW_CONSOLE_MUTE_JS]
        and vk_auth.VIEW_CONSOLE_NOISE == ("Not found blocking ",)
        and "Not found blocking " in vk_auth.VIEW_CONSOLE_MUTE_JS
        and "console.warn" in vk_auth.VIEW_CONSOLE_MUTE_JS
        # add_init_script вычисляет текст как выражение: без вызова стрелочная
        # функция только создалась бы, и шум остался бы в консоли.
        and vk_auth.VIEW_CONSOLE_MUTE_JS.lstrip().startswith("(() =>")
        and vk_auth.VIEW_CONSOLE_MUTE_JS.rstrip().endswith("})();"),
        f"окно глушит в консоли только шум статики VK: {open_ctx.scripts}",
    )

    # Экран окна — телефонный: так VK отдаёт ту же ленту лёгкой мобильной выдачей, и
    # листается она так же, как в телефоне (см. view_mobile_context()). Тем же экраном
    # открывается и лента в заходе через F5: это тоже «работа с лентой».
    check(
        open_ctx.context_args.get("viewport") == vk_auth.VIEW_VIEWPORT
        and open_ctx.context_args.get("is_mobile") is True
        and open_ctx.context_args.get("has_touch") is True
        and open_ctx.context_args.get("device_scale_factor") == vk_auth.VIEW_DEVICE_SCALE
        and vk_auth.is_mobile(open_ctx.context_args.get("user_agent") or ""),
        f"окно открывается мобильным экраном — лента как в телефоне: "
        f"{open_ctx.context_args}",
    )
    vk_src = (ROOT / "src" / "vk_auth.py").read_text(encoding="utf-8")
    check(
        vk_src.count("**view_mobile_context()") == 2,
        "мобильный экран ставят и окно обхода, и заход в ленту через F5 "
        f"(вход --login остаётся широким): {vk_src.count('**view_mobile_context()')} места",
    )
    check(
        vk_src.count("add_init_script(VIEW_CONSOLE_MUTE_JS)") == 2,
        "шум статики VK глушится и в окне обхода, и в окне F5 "
        f"(вход --login не трогаем): {vk_src.count('add_init_script(VIEW_CONSOLE_MUTE_JS)')} места",
    )

    class FakeContext:
        """Контекст окна: ловим, что именно подставили в браузер."""

        def __init__(self) -> None:
            self.cleared = 0
            self.jar: list[dict] = []

        def clear_cookies(self) -> None:
            self.cleared += 1

        def add_cookies(self, jar) -> None:
            self.jar = list(jar)

    vlogs.clear()
    session_file = tmp / "session_view_cookies.json"
    session_file.write_text(
        json.dumps({"cookies": {"remixsid": "abc", "httoken": "def"}}), encoding="utf-8"
    )
    paint_ctx = FakeContext()
    painter._context = paint_ctx
    painter._apply_cookies(str(session_file))
    check(
        paint_ctx.cleared == 1 and len(paint_ctx.jar) == 4
        and all(item["domain"] in vk_auth.VIEW_COOKIE_DOMAINS for item in paint_ctx.jar),
        f"окно ставит cookies сессии доменом (сессия жива и на поддоменах VK): {paint_ctx.jar[:1]}",
    )

    # Сессия без remixsid: окно предупреждает, что VK может показать вход.
    painter._warned_cookies = False
    vlogs.clear()
    no_login_file = tmp / "session_view_nologin.json"
    no_login_file.write_text(json.dumps({"cookies": {"p": "xyz"}}), encoding="utf-8")
    painter._apply_cookies(str(no_login_file))
    check(
        any("нет remixsid" in line for line in vlogs),
        f"сессия без remixsid — окно предупреждает про вход: {vlogs!r}",
    )

    # VK вместо ленты показал вход: окно это замечает, говорит один раз и просит
    # свежие cookies — показом окна обхода это уже не считает.
    login_page = FakePage(lands_on="https://login.vk.ru/?act=login")
    painter._page = login_page
    painter._warned_login = False
    painter._cookies_at = time.time()
    vlogs.clear()
    logins_before = painter.logins
    check(
        painter._render("маникюр", "подпись окна") is False
        and painter.logins == logins_before + 1,
        f"вход вместо ленты показом не считаем: входов {painter.logins}",
    )
    check(
        login_page.js == [
            (vk_auth.VIEW_OVERLAY_JS, "подпись окна" + vk_auth.VIEW_LOGIN_NOTE)
        ],
        f"подпись на странице входа сама говорит, что VK просит вход: {login_page.js}",
    )
    check(
        any("VK просит вход вместо ленты" in line for line in vlogs)
        and painter._cookies_at == 0.0,
        f"про вход окно пишет в лог и перечитает cookies на следующем окне: {vlogs!r}",
    )
    vlogs.clear()
    painter._render("маникюр", "подпись окна")
    check(
        painter.logins == logins_before + 2
        and not any("просит вход" in line for line in vlogs),
        "про вход окно говорит один раз — лог не засоряем",
    )

    # Пустая страница (прокси молчит) — это сбой окна, а не «показ ленты».
    painter._page = FakePage(lands_on="about:blank")
    try:
        painter._render("маникюр", "подпись окна")
    except vk_auth.VkAuthError as exc:
        check(
            "пустым" in str(exc),
            f"пустое окно — сбой окна, а не показ: {exc}",
        )
    else:
        check(False, "пустое окно посчитали показом обхода")
    painter._page = None
    painter._context = None
    vk_auth.log = orig_vk_log

    FakeViewer.made.clear()
    run_tags._viewer = None
    view_acc = type("Acc", (), {
        "login": "79990000009", "proxy": "http://u:p@1.1.1.1:80",
        "session_path": tmp / "session_79990000009.json", "status": READY,
    })()
    view_pool = FakePool([view_acc])
    show_args = run_tags.parse_args(["--show-browser"])
    viewer = run_tags.start_feed_viewer(view_pool, show_args, {})
    check(
        viewer is not None and viewer.started and viewer.label == "79990000009",
        f"окно обхода поднимается аккаунтом из пула: {viewer and (viewer.label, viewer.proxy)}",
    )
    check(
        viewer is not None and viewer.session_path == str(view_acc.session_path),
        "окно берёт сессию (и прокси) того аккаунта, которым заходит",
    )
    check(
        viewer is not None and viewer.tag_query == "hash",
        "без --tag-query окно ищет тег как раньше — хештегом: "
        f"{viewer and viewer.tag_query}",
    )
    job = {"id": 42, "tag": "маникюр"}
    run_tags.view_crawl_window(view_pool, show_args, {}, job, 1_700_000_000, 1_700_003_600, "79990000009")
    check(
        viewer is not None and viewer.visits == [(
            "маникюр",
            f"обход ленты: #маникюр  {run_tags.fmt_ts(1_700_000_000)}–"
            f"{run_tags.fmt_ts(1_700_003_600)}  окно #42  79990000009",
        )],
        f"окно подписано: тег, окно времени, номер окна, аккаунт: {viewer and viewer.visits}",
    )
    run_tags.view_crawl_window(view_pool, show_args, {}, {"id": 43, "tag": "ресницы"},
                               1_700_003_600, 1_700_007_200, "79990000009")
    check(
        viewer is not None and len(viewer.visits) == 2 and len(FakeViewer.made) == 1
        and run_tags.start_feed_viewer(view_pool, show_args, {}) is viewer,
        "новое окно времени идёт в то же окно: обход не открывает браузер на каждое окно",
    )
    run_tags.view_crawl_window(view_pool, show_args, {}, job, 1_700_000_000, 1_700_000_000, "")
    check(
        viewer is not None and len(viewer.visits) == 2,
        "вырожденное окно (без времени) в окне не показываем",
    )

    run_tags.stop_feed_viewer()
    check(
        viewer is not None and viewer.stopped and run_tags._viewer is None,
        "конец прогона закрывает окно обхода",
    )
    run_tags.stop_feed_viewer()
    check(True, "повторная остановка окна ничего не ломает")

    # Режим тега окно берёт из того же tag_query, что и запросы прогона: «без #» окно
    # открывает выдачу словом и так же подписывает её — видно, что собираем.
    word_args = run_tags.parse_args(["--show-browser", "--tag-query", "word"])
    word_viewer = run_tags.start_feed_viewer(view_pool, word_args, {})
    check(
        word_viewer is not None and word_viewer.tag_query == "word",
        f"с --tag-query word окно ищет тег словом: {word_viewer and word_viewer.tag_query}",
    )
    run_tags.view_crawl_window(
        view_pool, word_args, {}, job, 1_700_000_000, 1_700_003_600, "79990000009"
    )
    check(
        word_viewer is not None
        and word_viewer.visits == [(
            "маникюр",
            f"обход ленты: маникюр (словом)  {run_tags.fmt_ts(1_700_000_000)}–"
            f"{run_tags.fmt_ts(1_700_003_600)}  окно #42  79990000009",
        )],
        f"в «без #» окно подписано словом, а не хештегом: {word_viewer and word_viewer.visits}",
    )
    run_tags.stop_feed_viewer()
    settings_viewer = run_tags.start_feed_viewer(
        view_pool, run_tags.parse_args(["--show-browser"]), {"tag_query": "word"}
    )
    check(
        settings_viewer is not None and settings_viewer.tag_query == "word",
        "настройка tag_query доходит и до окна: "
        f"{settings_viewer and settings_viewer.tag_query}",
    )
    run_tags.stop_feed_viewer()

    check(
        run_tags.start_feed_viewer(view_pool, run_tags.parse_args([]), {}) is None,
        "без --show-browser окно обхода не открывается",
    )
    check(
        run_tags.start_feed_viewer(view_pool, run_tags.parse_args(["--show-browser", "--no-browser"]), None)
        is None,
        "с --no-browser окно обхода тоже не открываем (браузер выключен совсем)",
    )
    check(
        run_tags.start_feed_viewer(FakePool(), show_args, {}) is None,
        "годного аккаунта нет — показывать нечем, прогон идёт как обычно",
    )
    before = len(FakeViewer.made)
    run_tags.view_crawl_window(view_pool, run_tags.parse_args([]), {}, job, 1, 2, "")
    check(
        len(FakeViewer.made) == before and viewer is not None and len(viewer.visits) == 2,
        "без показа окно обхода ничего не получает (скрытый путь прежний)",
    )

    def broken_viewer(*_a, **_kw):
        """Окно не поднялось (нет браузера, нет экрана) — обход обязан идти дальше."""
        raise RuntimeError("браузера нет")

    vk_auth.FeedViewer = broken_viewer
    logs.clear()
    run_tags.view_crawl_window(view_pool, show_args, {}, job, 1, 2, "")
    check(
        any("окно на виду" in line for line in logs),
        f"сбой окна только пишет строку в лог, обход не срывает: {logs[-1:]!r}",
    )
    vk_auth.FeedViewer = FakeViewer

    # Настоящий FeedViewer, но без браузера: проверяем его поток — окна обхода идут в
    # него по одному, страница показывается, а сбой окна тихо выключает показ.
    real = orig_viewer(tmp / "session_view.json", "")
    seen: list[tuple[str, str]] = []

    def wait_for(pred, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end and not pred():
            time.sleep(0.02)
        return pred()

    real._session = lambda: contextlib.nullcontext(None)
    real._open = lambda pw: None
    real._apply_cookies = lambda path: None

    def show_crawl_page(tag, caption):
        """Лента открылась — это показ окна обхода (True у _render)."""
        seen.append((tag, caption))
        return True

    real._render = show_crawl_page
    real.start()
    real.visit("маникюр", "первое окно")
    check(wait_for(lambda: len(seen) == 1), f"поток окна показывает страницу обхода: {seen}")
    real.visit("ресницы", "второе окно")
    check(
        wait_for(lambda: len(seen) == 2 and real.shown == 2),
        f"следующее окно идёт в то же окно, показ считается: {seen} (показов {real.shown})",
    )

    fails = {"n": 0}

    def boom(tag, caption):
        fails["n"] += 1
        raise RuntimeError("страница обхода не открылась")

    real._render = boom
    for i in range(vk_auth.VIEW_MAX_FAIL_LOG):
        real.visit("маникюр", f"сбой {i}")
        wait_for(lambda i=i: fails["n"] > i)
    check(
        fails["n"] == vk_auth.VIEW_MAX_FAIL_LOG and not real.alive(),
        f"окно после {vk_auth.VIEW_MAX_FAIL_LOG} сбоев выключается само (обход идёт скрыто): "
        f"{fails['n']} сбоев, живо {real.alive()}",
    )
    real.visit("маникюр", "уже некому")
    wait_for(lambda: fails["n"] > vk_auth.VIEW_MAX_FAIL_LOG, timeout=0.3)
    check(fails["n"] == vk_auth.VIEW_MAX_FAIL_LOG, "сломанное окно больше не берёт окна обхода")
    real.stop()
    check(
        real._thread is not None and not real._thread.is_alive(),
        "stop() завершает поток окна (обход не оставляет браузер висеть)",
    )

    # Лента листается и между окнами обхода: прогон молчит — окно всё равно листает
    # (шаг 0.2–0.45 с), посты догружаются, подпись обновляется, а показом окна обхода
    # это не считается (shown не растёт).
    ticker = orig_viewer(tmp / "session_scroll.json", "")
    tick_page = FakePage()
    ticker._session = lambda: contextlib.nullcontext(None)
    ticker._open = lambda pw: None
    ticker._apply_cookies = lambda path: None
    ticker._page = tick_page
    ticker._tag = "маникюр"
    ticker._caption = "обход ленты: #маникюр"
    ticker.start()
    started = time.time()
    check(
        wait_for(lambda: len(tick_page.scrolls) >= 3, timeout=4.0),
        f"окно листает ленту, даже когда новых окон обхода нет: "
        f"прокруток {len(tick_page.scrolls)}",
    )
    elapsed = max(time.time() - started, 0.01)
    check(
        len(tick_page.scrolls) <= elapsed / vk_auth.VIEW_SCROLL_MIN_SEC + 2,
        f"прокрутки идут не чаще паузы {vk_auth.VIEW_SCROLL_MIN_SEC} с: "
        f"{len(tick_page.scrolls)} прокруток за {elapsed:.2f} с",
    )
    check(
        ticker.shown == 0 and tick_page.js and tick_page.js[-1] == (
            vk_auth.VIEW_OVERLAY_JS,
            "обход ленты: #маникюр" + vk_auth.VIEW_POSTS_NOTE.format(n=8),
        ),
        f"листание между окнами обхода показом не считается: показов {ticker.shown}, "
        f"подпись {tick_page.js[-1] if tick_page.js else '—'}",
    )
    ticker.stop()
    check(
        not ticker.alive() and not ticker._thread.is_alive(),
        "окно, которое только листало ленту, закрывается по stop()",
    )

    # 7. Команда запуска (её же собирают окно и панель) и сама панель.
    check(
        "--show-browser" in gui.build_run_args(show_browser=True),
        f"в команду прогона добавляется --show-browser: {gui.build_run_args(show_browser=True)}",
    )
    check(
        "--show-browser" not in gui.build_run_args(),
        "без галочки команда остаётся прежней (показ включается только явно)",
    )
    index_html = (ROOT / "src" / "webui" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "src" / "webui" / "app.js").read_text(encoding="utf-8")
    check('id="run-showbrowser"' in index_html, "на странице панели есть галочка показа окна")
    check(
        'show_browser: $("run-showbrowser").checked' in app_js,
        "панель отправляет show_browser в запросе на «Старт»",
    )
    check(
        '$("run-showbrowser").checked = Boolean(settings.show_browser)' in app_js,
        "галочка подставляется из сохранённой настройки show_browser",
    )
finally:
    settings_mod.CONFIG_FILE = orig_config
    run_tags.has_display = orig_display
    vk_auth.FeedViewer = orig_viewer
    run_tags._viewer = None
    sys.argv = list(orig_argv)
    if db is not None:
        db.close()
    shutil.rmtree(tmp, ignore_errors=True)

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ВИДНО И ЭМУЛЯТОР, И ОБХОД ЛЕНТЫ")
raise SystemExit(1 if FAILED else 0)

