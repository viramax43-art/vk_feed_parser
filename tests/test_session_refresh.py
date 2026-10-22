#!/usr/bin/env python3
"""Проверка фонового обновления сессии: токен меняется, пока идёт прогон.

    python tests/test_session_refresh.py

Сеть и браузер не нужны: обмен cookies и профиль браузера подменяются заглушками,
файлы — во временной папке. Проверяем, что фон раз в session_refresh_minutes берёт
живые аккаунты пула, получает свежий токен и подставляет его работающему клиенту
(мёртвых не трогает), что при отказе HTTP-обмена («too much tries») токен идём
добывать через профиль браузера с F5 и аккаунт остаётся на этом пути, что обновление
не теряет профиль браузера из сессии, что лента действительно обновляется через F5
(reload, при отказе — клавиша), что токен около срока годности обновляется досрочно,
что --no-browser отключает браузер и что «0» в настройке выключает обновление совсем.
Отдельно проверяем, что «ВК молча отдаёт пустую выдачу» лечится сменой запроса: воркер
освежает сессию (F5), повторяет то же окно другим count, дробит его пополам и отдаёт
другому аккаунту — и только после нескольких РАЗНЫХ пустых окон уходит в отлёжку. Пауз
на пустой выдаче нет: окно не крутится на месте, а остаётся базе до следующего «Старта».
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import run_tags  # noqa: E402
import vk_auth  # noqa: E402
from account_pool import DEAD, READY  # noqa: E402
from accounts import load_accounts  # noqa: E402
from settings import DEFAULTS, load_settings  # noqa: E402
from state_db import DB  # noqa: E402

PHONES = ("+79990000001", "+79990000002")
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def write_session(session_path: Path, token: str, expires_in: int = 86400) -> dict:
    """Пишет файл сессии так, как это делает vk_auth (токен + срок годности)."""
    payload = {
        "access_token": token,
        "token_expires_at": int(time.time()) + int(expires_in),
        "api_version": "5.289",
        "login": Path(session_path).stem.replace("session_", ""),
    }
    Path(session_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


# 1. аккаунты, их сессии и пул
tmp = Path(tempfile.mkdtemp(prefix="session_refresh_"))
accounts_file = tmp / "accounts.txt"
accounts_file.write_text(
    "# телефон;пароль;2FA;прокси\n"
    + "\n".join(
        f"{phone};пароль;;{i + 1}.{i + 1}.{i + 1}.{i + 1}:80:u:p"
        for i, phone in enumerate(PHONES)
    )
    + "\n",
    encoding="utf-8",
)
accounts = load_accounts(accounts_file)
check(len(accounts) == 2, f"в файле два аккаунта: {[a.login for a in accounts]}")
for acc in accounts:
    # Сессии — во временной папке: проверка не трогает data/session_*.json.
    acc.session_file = tmp / f"session_{re.sub(r'[^0-9]', '', acc.login)}.json"
    write_session(acc.session_file, f"vk1.a.старый-{acc.login}")

db = DB(tmp / "parser.sqlite3")
pool, workers = run_tags.build_pool(accounts, db, 2, "")
check(workers == 2, f"живых аккаунтов хватает воркерам: {workers}")
check(len(pool.items()) == 2, f"пул отдаёт аккаунты целиком: {len(pool.items())}")
check(
    all(a.session_path and a.proxy.startswith("http://") for a in pool.items()),
    "фону видны и файл сессии, и прокси аккаунта",
)

# 2. обмен cookies/браузера подменяем заглушками, лог перехватываем
calls = {"cookies": [], "browser": []}


def fake_cookies(session_path, proxy_raw=""):
    calls["cookies"].append(Path(session_path).name)
    return write_session(session_path, f"vk1.a.новый-{len(calls['cookies'])}")


def fake_browser(session_path, proxy_raw="", timeout=90):
    calls["browser"].append(Path(session_path).name)
    return write_session(session_path, f"vk1.a.браузер-{len(calls['browser'])}")


def boom(*_args, **_kwargs):
    raise RuntimeError("too much tries")


def no_cookies(*_args, **_kwargs):
    raise RuntimeError("в сессии нет remixsid — нужен повторный вход")


def browser_throttled(*_args, **_kwargs):
    """Профиль браузера тоже упёрся в «too much tries» — проверяем отказ F5-пути."""
    calls["browser"].append("throttled")
    raise RuntimeError("too much tries (лента не открылась)")


# Настоящие функции обновления: тесты их подменяют, но сами они тоже проверяются (кейсы 14–16).
REAL_REFRESH_FROM_COOKIES = vk_auth.refresh_from_cookies
REAL_REFRESH_VIA_BROWSER = vk_auth.refresh_via_browser

vk_auth.refresh_from_cookies = fake_cookies
vk_auth.refresh_via_browser = boom
logs: list[str] = []
run_tags.log = logs.append

refresher = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 2})
check(refresher.interval == 120.0, f"интервал берётся из настроек: {refresher.interval} с")
check(
    [a.login for a in refresher.accounts()] == list(PHONES),
    f"в фон идут живые аккаунты: {[a.login for a in refresher.accounts()]}",
)

# 3. работающий клиент получает свежий токен, файл сессии перезаписан
client = type("Client", (), {"token": "vk1.a.старый", "api_version": "5.199"})()
run_tags.register_client(PHONES[0], client)
acc0 = pool.items()[0]
check(refresher.refresh_due(acc0) is True, "по сроку сессия обновилась")
check(
    client.token.startswith("vk1.a.новый"),
    f"клиент воркера получил свежий токен: {client.token}",
)
check(client.api_version == "5.289", f"версия API из сессии: {client.api_version}")
check(
    json.loads(Path(acc0.session_path).read_text(encoding="utf-8"))["access_token"]
    == client.token,
    "файл сессии перезаписан тем же токеном",
)
check(calls["cookies"] == [Path(acc0.session_path).name], f"обменов: {calls['cookies']}")
check(not calls["browser"], "пока cookies принимают — браузер не открываем")
check(refresher.done == 1, f"успешных обменов: {refresher.done}")
check(
    any("сессия обновлена" in line and "каждые 2.0 мин" in line for line in logs),
    f"в логе видно, что токен меняется: {logs[-1] if logs else '—'}",
)

# 4. внутри интервала VK не дёргаем
check(refresher.refresh_due(acc0) is None, "второй раз сразу после обмена — пропуск")
check(len(calls["cookies"]) == 1, f"запросов к VK за это время: {len(calls['cookies'])}")

# 5. срок прошёл — меняем снова (и повторный успех в лог не льётся)
refresher._next_at[acc0.login] = time.time() - 1
check(refresher.refresh_due(acc0) is True, "после интервала токен снова меняется")
check(len(calls["cookies"]) == 2, f"обменов стало: {len(calls['cookies'])}")
check(
    len([line for line in logs if "сессия обновлена" in line]) == 1,
    "успехи не заливают лог: пишем раз в 10 минут",
)

# 6. «too much tries»: прогон не роняем, уходим в профиль браузера (F5), отступаем на 5 минут
vk_auth.refresh_from_cookies = boom
vk_auth.refresh_via_browser = browser_throttled
token_before = client.token
before_browser = list(calls["browser"])
refresher._next_at[acc0.login] = time.time() - 1
check(refresher.refresh_due(acc0) is False, "отказ обмена виден как False")
check(client.token == token_before, "старый токен не подменили пустым")
check(
    calls["browser"] == before_browser + ["throttled"],
    "при «too much tries» токен идём добывать через профиль браузера (лента + F5)",
)
check(
    refresher._prefer_browser_until.get(acc0.login, 0.0) > time.time(),
    "аккаунт переведён на браузерное обновление, пока VK режет HTTP-обмен",
)
check(
    refresher._next_at[acc0.login] - time.time()
    > run_tags.REFRESH_THROTTLE_BACKOFF_SEC - 10,
    "VK притормозил обмен — отступаем на 5 минут, а не долбим каждую минуту",
)
check(
    any("сессию обновить не вышло" in line and "притормозил" in line for line in logs),
    f"причина отказа попала в лог: {logs[-1] if logs else '—'}",
)
check(
    any("F5" in line for line in logs),
    f"в логе видно, что пробовали профиль браузера с F5: {logs[-1] if logs else '—'}",
)

# 6.1. сколько ждать после отказа: троттлинг — 5 мин, прочее — обычный интервал
check(
    refresher._retry_in("cookies: too much tries") == run_tags.REFRESH_THROTTLE_BACKOFF_SEC,
    "«too much tries» — пауза 5 минут",
)
check(
    refresher._retry_in("cookies: соединение сброшено") == refresher.interval,
    f"прочий отказ — повтор через интервал (у нас {refresher.interval} с)",
)

# 6.2. аккаунт остаётся на браузерном обновлении: первым идёт профиль+F5, HTTP не трогаем
vk_auth.refresh_via_browser = fake_browser
before_cookies = list(calls["cookies"])
refresher._next_at[acc0.login] = time.time() - 1
check(refresher.refresh_due(acc0) is True, "пока VK режет HTTP, сессию обновляет браузер")
check(
    calls["cookies"] == before_cookies,
    f"HTTP-обмен для этого аккаунта больше не дёргаем: {calls['cookies']}",
)
check(client.token.startswith("vk1.a.браузер"), f"токен из браузера: {client.token}")
refresher._prefer_browser_until.clear()  # дальше снова проверяем обычный порядок

# 7. cookies не приняты, но связь жива — токен берём через профиль браузера
vk_auth.refresh_from_cookies = no_cookies
vk_auth.refresh_via_browser = fake_browser
refresher._next_at[acc0.login] = time.time() - 1
check(refresher.refresh_due(acc0) is True, "через профиль браузера токен получен")
check(
    calls["browser"] and calls["browser"][-1] == Path(acc0.session_path).name,
    f"браузер открылся на этом аккаунте: {calls['browser']}",
)
check(client.token.startswith("vk1.a.браузер"), f"токен из браузера: {client.token}")

# 8. --no-browser: браузер не открываем вовсе
no_browser = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 2}, no_browser=True)
before_browser = list(calls["browser"])
no_browser._next_at[PHONES[0]] = time.time() - 1
vk_auth.refresh_from_cookies = no_cookies
check(no_browser.refresh_due(acc0) is False, "без браузера и с отказом cookies обмена нет")
check(calls["browser"] == before_browser, f"браузер не открывался: {calls['browser']}")

# 9. мёртвые аккаунты фон не трогает
db.mark_account(PHONES[1], status=DEAD)
dead_pool, _ = run_tags.build_pool(accounts, db, 2, "")
dead_refresher = run_tags.SessionRefresher(dead_pool, {"session_refresh_minutes": 2})
check(
    [a.login for a in dead_refresher.accounts()] == [PHONES[0]],
    f"мёртвый аккаунт вне фона: {[a.login for a in dead_refresher.accounts()]}",
)
check(
    all(item["status"] in (READY, DEAD) for item in dead_pool.snapshot()),
    "статусы пула не сломались",
)

# 10. токен вот-вот истечёт — обновляем досрочно, не дожидаясь интервала
vk_auth.refresh_from_cookies = fake_cookies
long_interval = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 60})
write_session(acc0.session_path, "vk1.a.скоро-истечёт", expires_in=120)
long_interval._next_at[acc0.login] = time.time() + 3600
check(long_interval.refresh_due(acc0) is True, "истекающий токен обновляем раньше срока")

# 11. воркер забывает клиент: подставлять токен больше некому
run_tags.forget_client(PHONES[0], client)
check(
    run_tags.apply_token(PHONES[0], "vk1.a.чужой") == 0,
    "после release клиент из реестра убран",
)
run_tags.register_client(PHONES[1], client)
check(run_tags.apply_token("", "vk1.a.общий") == 1, "токен подставляется всем клиентам реестра")
run_tags.forget_client(PHONES[1])

# 12. поток работает фоном и останавливается вместе с прогоном
run_tags.MIN_REFRESH_INTERVAL_SEC = 0.05
run_tags._stop_flag.clear()
try:
    fast = run_tags.SessionRefresher(pool, {"session_refresh_minutes": 0.001})
    check(fast.interval >= 0.05, f"интервал не мельче минимума: {fast.interval} с")
    fast.start()
    time.sleep(0.4)
    run_tags._stop_flag.set()
    fast.join(timeout=5)
    check(fast.done >= 2, f"фон обновил сессии сам, без участия человека: {fast.done} обменов")
    check(not fast.is_alive(), "поток останавливается вместе с прогоном")
finally:
    run_tags.MIN_REFRESH_INTERVAL_SEC = 30.0
    run_tags._stop_flag.clear()

# 13. настройка и флаги: 0 — выключено, флаг важнее настройки
settings = load_settings()
check(
    DEFAULTS.get("session_refresh_minutes") == 15,
    f"по умолчанию обновляем каждые 15 мин: {DEFAULTS.get('session_refresh_minutes')}",
)
check(
    float(settings.get("session_refresh_minutes") or 0) >= 0,
    f"настройка читается из data/settings.json: {settings.get('session_refresh_minutes')}",
)
check(
    run_tags.session_refresh_minutes(
        run_tags.parse_args([]), {"session_refresh_minutes": 2}
    ) == 2.0,
    "без флагов берётся настройка",
)
check(
    run_tags.session_refresh_minutes(
        run_tags.parse_args(["--session-refresh-minutes", "5"]), {"session_refresh_minutes": 2}
    ) == 5.0,
    "флаг --session-refresh-minutes важнее настройки",
)
check(
    run_tags.session_refresh_minutes(
        run_tags.parse_args(["--no-session-refresh"]), {"session_refresh_minutes": 2}
    ) == 0.0,
    "флаг --no-session-refresh выключает обновление",
)
check(
    run_tags.session_refresh_minutes(
        run_tags.parse_args([]), {"session_refresh_minutes": 0}
    ) == 0.0,
    "«session_refresh_minutes: 0» — обновления нет",
)

# 14. обновление через профиль браузера: сначала лента и F5, полный вход — только если F5 не помог
f5_calls: dict = {}


def fake_feed_reload(session_path, proxy_raw="", timeout=90, headless=True):
    f5_calls["f5"] = Path(session_path).name
    f5_calls["headless"] = headless  # скрыт ли браузер: видно, дошёл ли до него показ окна
    return {"access_token": "vk1.a.через-F5", "profile_dir": "…"}


def fake_interactive(*_args, **_kwargs):
    f5_calls["login"] = True
    return {"access_token": "vk1.a.через-вход"}


orig_feed_reload, orig_interactive = vk_auth.token_via_feed_reload, vk_auth.login_interactive
vk_auth.token_via_feed_reload = fake_feed_reload
vk_auth.login_interactive = fake_interactive
try:
    got = REAL_REFRESH_VIA_BROWSER(acc0.session_path, "")
    check(got["access_token"] == "vk1.a.через-F5", f"токен взяли после F5 на ленте: {got}")
    check("login" not in f5_calls, "полный вход не понадобился, раз F5 сработал")
    check(
        f5_calls.get("headless") is True,
        f"по умолчанию профиль браузера идёт скрыто (headless): {f5_calls.get('headless')}",
    )
    got = REAL_REFRESH_VIA_BROWSER(acc0.session_path, "", headless=False)
    check(
        f5_calls.get("headless") is False and got["access_token"] == "vk1.a.через-F5",
        "show_browser доходит до эмулятора: headless=False (окно на виду)",
    )

    def f5_fails(*_args, **_kwargs):
        raise vk_auth.VkAuthError("лента не открылась: профиль не вошёл")

    vk_auth.token_via_feed_reload = f5_fails
    profile_dir = tmp / "vk_browser_profile_fallback"
    profile_dir.mkdir(exist_ok=True)
    data = json.loads(Path(acc0.session_path).read_text(encoding="utf-8"))
    data["profile_dir"] = str(profile_dir)
    Path(acc0.session_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    got = REAL_REFRESH_VIA_BROWSER(acc0.session_path, "")
    check(f5_calls.get("login") is True, "F5 не помог — уходим на полный вход в профиль")
    check(
        got["access_token"] == "vk1.a.через-вход",
        f"токен получен полным входом: {got['access_token']}",
    )
finally:
    vk_auth.token_via_feed_reload, vk_auth.login_interactive = orig_feed_reload, orig_interactive

# 15. «обновление ленты» действительно жмёт F5 (reload, при отказе — клавиша)


class FakePage:
    """Страница без браузера: помним адрес, перезагрузки и нажатые клавиши."""

    class Keyboard:
        def __init__(self, page):
            self._page = page

        def press(self, key):
            self._page.keys.append(key)

    def __init__(self, *, reload_ok: bool = True, blank: bool = False):
        self.url = "about:blank"
        self.reload_ok = reload_ok
        self.blank = blank
        self.reloaded = 0
        self.keys: list[str] = []
        self.keyboard = self.Keyboard(self)

    def goto(self, url, wait_until=None, timeout=None):
        if not self.blank:  # blank — страница так и осталась на about:blank
            self.url = url

    def reload(self, wait_until=None, timeout=None):
        self.reloaded += 1
        if not self.reload_ok:
            raise RuntimeError("Timeout 30000ms exceeded (reload)")

    def wait_for_load_state(self, *_args, **_kwargs):
        return None


page = FakePage()
ok, error = vk_auth._reload_feed(page, 5000)
check(ok and page.reloaded == 1, f"лента обновлена перезагрузкой (F5): {page.reloaded}")
check(page.url.endswith("/feed"), f"открылась именно лента: {page.url}")

page2 = FakePage(reload_ok=False)
ok2, error2 = vk_auth._reload_feed(page2, 5000)
check(ok2 and page2.keys == ["F5"], f"reload не сработал — жмём клавишу F5: {page2.keys}")

page3 = FakePage(reload_ok=False, blank=True)
ok3, error3 = vk_auth._reload_feed(page3, 5000)
check(not ok3, "пустое окно не выдаётся за обновлённую ленту")
check(
    page3.keys == [] and page3.reloaded == 0,
    "на неоткрытой странице F5 не жмём — сначала нужна сама лента",
)
check("about:blank" in error3, f"причина отказа ленты видна в отчёте: {error3[:80]!r}")

# 16. обмен cookies не выкидывает профиль браузера из сессии (иначе F5-путь пропадал)
keep_path = tmp / "session_keep.json"
write_session(keep_path, "vk1.a.старый")
keep_profile = tmp / "vk_browser_profile_keep"
keep_profile.mkdir(exist_ok=True)
keep = json.loads(keep_path.read_text(encoding="utf-8"))
keep["profile_dir"] = str(keep_profile)
keep["cookies"] = {"remixsid": "x" * 12}
keep_path.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")

orig_token_http, orig_validate = vk_auth.request_web_token_http, vk_auth.validate_token
vk_auth.request_web_token_http = lambda *_a, **_kw: {
    "access_token": "vk1.a.свежий-cookies",
    "expires_in": 3600,
    "user_id": 7,
}
vk_auth.validate_token = lambda *_a, **_kw: {"id": 7, "first_name": "Тест", "last_name": ""}
try:
    payload = REAL_REFRESH_FROM_COOKIES(keep_path, "")
finally:
    vk_auth.request_web_token_http = orig_token_http
    vk_auth.validate_token = orig_validate
check(payload["access_token"] == "vk1.a.свежий-cookies", "обмен cookies сохранил новый токен")
check(
    payload.get("profile_dir") == str(keep_profile),
    f"профиль браузера не потерялся: {payload.get('profile_dir')}",
)
on_disk = json.loads(keep_path.read_text(encoding="utf-8"))
check(
    on_disk.get("profile_dir") == str(keep_profile),
    "в файле сессии профиль браузера тоже на месте",
)
check(on_disk.get("source") == "cookies", f"источник обновления записан: {on_disk.get('source')}")

# 17. «ВК молча отдаёт пустую выдачу» (канарейка подтвердила ограничение) — лечим F5
# в профиле браузера: сессия освежается, и то же окно берём заново, без пауз.


class EmptyPool:
    """Пул-заглушка: воркер только пишет в него добытые посты."""

    def __init__(self) -> None:
        self.posts = 0

    def add_posts(self, acc, count) -> None:
        self.posts += int(count or 0)


class FakeClient:
    """Клиент аккаунта: для этой проверки воркеру нужны только токен и версия API."""

    def __init__(self, login: str) -> None:
        self.login = login
        self.token = "vk1.a.старый"
        self.api_version = "5.199"


f5 = {"calls": 0, "fail": False}


def fake_f5_refresh(session_path, proxy_raw="", timeout=90, headless=True):
    """F5 в профиле: как настоящий vk_auth пишет в файл сессии свежий токен.

    headless принимаем как настоящий vk_auth: в настройках может стоять
    show_browser, тогда прогон зовёт F5 с headless=False.
    """
    f5["calls"] += 1
    if f5["fail"]:
        raise vk_auth.VkAuthError("в профиле нет remixsid — нужен вход --login")
    return write_session(session_path, "vk1.a.после-F5")


windows = {"calls": 0, "empty_left": 1}
per_window: dict[tuple, int] = {}  # сколько раз запрашивали каждое окно


def fake_window(client, db, tag, **kw):
    """Сначала пусто (тихое ограничение), после F5 — посты."""
    windows["calls"] += 1
    key = (tag, kw.get("start_time"), kw.get("end_time"))
    per_window[key] = per_window.get(key, 0) + 1
    if windows["calls"] <= windows["empty_left"]:
        return {"status": "ok", "reason": "end_of_feed", "seen": 0, "new": 0, "pages": 1,
                "need_split": False, "cursor": ""}
    return {"status": "ok", "reason": "end_of_feed", "seen": 12, "new": 12, "pages": 1,
            "need_split": False, "cursor": ""}


empty_login = "79990000099"
empty_session = tmp / f"session_{empty_login}.json"
write_session(empty_session, "vk1.a.старый")
empty_acc = type("Acc", (), {
    "login": empty_login,
    "proxy": "http://u:p@1.1.1.1:80",
    "session_path": empty_session,
    "cooldown_until": 0.0,
})()
empty_args = run_tags.parse_args([])
empty_args.dry_run = False
empty_args.no_export = True
empty_args.tag_filter = ""
# Границы периода ставит resolve_period; здесь окна разные, чтобы каждое было свежим
# (у закрытого окна fetch_bounds читает именно эти поля).
empty_args.start_time = 0
empty_args.end_time = 0
empty_db = DB(tmp / "parser_empty.sqlite3")
W17A = (1_700_000_000, 1_700_003_600)
W17B = (1_700_003_600, 1_700_007_200)
W17C = (1_700_007_200, 1_700_010_800)
W17D = (1_700_010_800, 1_700_014_400)
W17E = (1_700_014_400, 1_700_018_000)

orig_window17, orig_throttle17 = run_tags.probe_mod.run_window, run_tags.empty_is_throttle
orig_export17 = run_tags.maybe_live_export
orig_feed_reload17 = vk_auth.token_via_feed_reload
run_tags.probe_mod.run_window = fake_window
run_tags.empty_is_throttle = lambda *_a, **_kw: True  # канарейка: посты есть, а выдача пуста
run_tags.maybe_live_export = lambda *_a, **_kw: None
vk_auth.token_via_feed_reload = fake_f5_refresh
try:
    queue17 = run_tags.JobQueue()
    queue17.put(empty_db.add_job("маникюр", *W17A, 1))
    empty_client = FakeClient(empty_login)
    empty_logs = len(logs)
    outcome, reason = run_tags.work_account(
        empty_client, empty_acc, queue17, empty_args, settings, empty_db, EmptyPool()
    )
    fresh = logs[empty_logs:]
    check(outcome == "empty", f"окно взяли свежей сессией, очередь опустела: {outcome}/{reason}")
    check(f5["calls"] == 1, f"на пустую выдачу жмём F5 в профиле: {f5['calls']} раз")
    check(windows["calls"] == 2, f"окно перезапросили сразу, а не паузой: запросов {windows['calls']}")
    check(
        empty_client.token == "vk1.a.после-F5" and empty_client.api_version == "5.289",
        f"клиент продолжил работу новым токеном: {empty_client.token} / {empty_client.api_version}",
    )
    check(
        json.loads(Path(empty_session).read_text(encoding="utf-8"))["access_token"]
        == "vk1.a.после-F5",
        "файл сессии тоже обновлён — фон увидит свежий токен",
    )
    check(
        any("F5" in line for line in fresh),
        f"в логе видно, что жали F5: {[l for l in fresh if 'F5' in l][:1]}",
    )
    check(
        not [line for line in fresh if "пауза" in line],
        f"пауза на пустой выдаче не понадобилась: {fresh}",
    )
    check(
        run_tags.browser_preferred(empty_login),
        "аккаунт переведён на F5-путь — фон дальше тоже без HTTP-обмена",
    )
    check(
        refresher._prefer_browser_until.get(empty_login, 0.0) > time.time(),
        "фон обновления видит этот перевод (список общий с воркерами)",
    )

    # F5 не помог (профиль уже не вошёл): паузы не крутим — меняем сам запрос (другой
    # count), потом делим окно пополам, потом окно достаётся другому аккаунту. Только
    # после нескольких РАЗНЫХ пустых окон аккаунт уходит в отлёжку.
    f5.update(calls=0, fail=True)
    windows.update(calls=0, empty_left=99)
    per_window.clear()
    queue17b = run_tags.JobQueue()
    for lo, hi in (W17B, W17D, W17E):
        queue17b.put(empty_db.add_job("маникюр", lo, hi, 1))
    empty_logs = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(empty_login), empty_acc, queue17b, empty_args, settings, empty_db, EmptyPool()
    )
    fresh = logs[empty_logs:]
    check(
        outcome == "hold" and reason == "ВК отдаёт пустую выдачу",
        f"пусто на разных окнах подряд — аккаунт в отлёжку: {outcome}/{reason}",
    )
    check(
        f5["calls"] == run_tags.EMPTY_F5_RETRIES,
        f"F5 пробуем ограниченное число раз на серию пустых выдач: {f5['calls']}",
    )
    check(
        any("F5 в профиле не помог" in line for line in fresh),
        f"в логе видно и причину отказа F5: {[l for l in fresh if 'не помог' in l][:1]}",
    )
    change17 = next(
        (re.search(r"count (\d+) → (\d+), без паузы", line)
         for line in fresh if "другим запросом" in line),
        None,
    )
    check(
        change17 is not None and int(change17.group(2)) < int(change17.group(1)),
        "на пустой выдаче меняем сам запрос (другой count), а не ждём: "
        f"{change17.group(0) if change17 else fresh[:3]}",
    )
    check(
        any("делю окно пополам" in line for line in fresh),
        f"пусто и на другом запросе — окно дробим: {[l for l in fresh if 'пополам' in l][:1]}",
    )
    check(
        any("оставляю следующему «Старту»" in line for line in fresh),
        f"окно не крутится на месте, а остаётся базе: "
        f"{[l for l in fresh if 'Старту' in l][:1]}",
    )
    check(
        not [line for line in fresh if "пауза" in line],
        f"пауз на пустой выдаче больше нет: {[l for l in fresh if 'пауза' in l][:2]}",
    )
    check(
        max(per_window.values(), default=0) <= 4,
        f"одно окно запрашивается ограниченное число раз: {per_window}",
    )
    check(
        empty_db.count_open_jobs("маникюр") > 0,
        "пустые окна остаются в базе pending — их добьёт следующий «Старт»",
    )

    # --no-browser остаётся выключателем браузера: F5 не жмём, сразу меняем запрос.
    # Аккаунт здесь свежий: на прежнем после серии пустых выдач включился «тихий» режим
    # (лестницу он больше не повторяет — счётчики живут на аккаунт весь прогон).
    no_browser_args = run_tags.parse_args(["--no-browser"])
    no_browser_args.dry_run = False
    f5.update(calls=0, fail=False)
    windows.update(calls=0, empty_left=99)
    per_window.clear()
    fresh_login = "79990000123"
    fresh_acc = type("Acc", (), {
        "login": fresh_login,
        "proxy": "http://u:p@1.1.1.1:80",
        "session_path": empty_session,
        "cooldown_until": 0.0,
    })()
    queue17c = run_tags.JobQueue()
    queue17c.put(empty_db.add_job("маникюр", *W17C, 1))
    empty_logs = len(logs)
    outcome, reason = run_tags.work_account(
        FakeClient(fresh_login), fresh_acc, queue17c, no_browser_args, settings, empty_db,
        EmptyPool()
    )
    fresh = logs[empty_logs:]
    check(f5["calls"] == 0, f"с --no-browser браузер не открываем: вызовов F5 {f5['calls']}")
    check(
        outcome == "empty",
        f"без браузера окно тоже не крутится паузами (аккаунт цел): {outcome}/{reason}",
    )
    check(
        any("другим запросом" in line for line in fresh) and
        not [line for line in fresh if "пауза" in line],
        f"и там же меняем запрос вместо пауз: {[l for l in fresh if 'запрос' in l][:1]}",
    )
finally:
    run_tags.probe_mod.run_window = orig_window17
    run_tags.empty_is_throttle = orig_throttle17
    run_tags.maybe_live_export = orig_export17
    vk_auth.token_via_feed_reload = orig_feed_reload17
    run_tags._browser_prefer_until.clear()

shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ОБНОВЛЕНИЕ СЕССИИ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)


