#!/usr/bin/env python3
"""Авторизация VK и получение user token из сессии браузера.

VK ID не даёт войти одним HTTP-запросом: нужен реальный браузер, и почти всегда на
шаге подтверждения появляется капча, код или QR. Поэтому вход интерактивный: окно
Chromium открывается, скрипт сам подставляет телефон и пароль, а капчу (код из
приложения или QR) завершает человек в открытом окне. Как только на домене vk.ru
появляется remixsid, скрипт забирает cookies, выменивает их на user token
(login.vk.ru/?act=web_token, app_id=7879029 — тот же токен, что использует сайт VK)
и сохраняет data/session.json.

Шаги интерактивного входа (актуальный VK ID):
   1) открывается vk.ru/?u=2&to=… и подставляется телефон → button[data-test-id="submit_btn"];
   2) капча «я не робот» — её решает человек (клик по label с not-robot-captcha-checkbox);
   3) страница подтверждения входа (data-page="MODULE_VERIFICATION", data-test-id="otp-title"):
      код (SMS/MAX) не ждём — скрипт сам жмёт «Войти при помощи пароля»
      (button.vkc__Bottom-module__switchToPassword) или «Подтвердить другим способом»
      (button[data-test-id="other-verification-methods"]);
   4) страница пароля (form.vkc__EnterPasswordNoUserInfo-module__content) — подставляется
      пароль и нажимается «Продолжить».
У части аккаунтов шаг 3 пропускается: после номера пароль спрашивают сразу.

Запуск:
    python src/vk_auth.py --login      # вход в браузере (капчу решает человек)
    python src/vk_auth.py --qr         # то же, но сразу предлагает QR-код
    python src/vk_auth.py --refresh    # обновить токен: профиль браузера (лента + F5), затем cookies
    python src/vk_auth.py --check      # проверить сессию и тестовый поиск
    python src/vk_auth.py --status     # что лежит в data/session.json

Токен обновляется без человека, и первым делом — через профиль браузера: скрипт
открывает сохранённый профиль (скрыто), заходит в ленту и жмёт F5, забирает cookies,
которые VK выдал живой сессии, и меняет их на access_token сетевым стеком Chromium
(refresh_via_browser → token_via_feed_reload). Для VK это обычный заход в ленту,
поэтому путь проходит даже когда HTTP-обмен cookies режется ошибкой «too much tries»;
фон прогона (src/run_tags.py) при таком отказе тоже уходит на этот путь. Если профиль
уже не вошёл (капча/код) — остаётся ручной вход --login.

Окно этого эмулятора можно смотреть: у фоновых путей есть параметр headless, а у
прогона — настройка show_browser и флаг --show-browser (в логе «браузер открыт на
виду»). Тогда браузер открывается видимым, после F5 на ленте он держится несколько
секунд — видно, что именно делает эмулятор, — и закрывается сам.

Если у аккаунта нет доступа к коду (SMS, MAX), помогает --password-only: скрипт
подставляет номер и пароль, а все подтверждения (капчу, «Пройти проверку») делает
человек, без автозапроса кодов. VK пускает по паролю 1–2 аккаунта с одного IP,
поэтому второму аккаунту нужен свежий прокси и отдельный профиль:
python src/vk_auth.py --account 2 --password-only --profile data/vk_browser_profile_2

Профиль браузера по умолчанию — data/vk_browser_profile; у аккаунта со своим
профилем — data/vk_browser_profile_<номер>, повторный вход нужен редко.

Если окно входа осталось пустым (about:blank), а в логе «Timeout … waiting until
"domcontentloaded"» или «net::ERR_PROXY_CONNECTION_FAILED» — виноват прокси аккаунта:
скрипт сам повторит открытие страницы несколько раз и, если так и не выйдет, скажет об
этом прямо (без трейсбека), а не оставит пустое окно на 15 минут.

Платформы: браузер для входа выбирается по системе (см. platform_compat) — Windows:
Edge/Chrome; Linux/сервер: встроенный Chromium от `playwright install chromium`, затем
системные Chrome/Chromium; под root автоматически добавляется --no-sandbox. Видимый
вход требует графической сессии: на сервере без X либо запускайте через
`xvfb-run -a python3 src/vk_auth.py --login`, либо входите токеном LOGIN:PASS:TOKEN,
либо перенесите готовый data/session_*.json с машины с экраном.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import (  # noqa: E402
    Account,
    load_accounts,
    normalize_proxy,
    proxy_host_port,
    proxy_to_playwright,
    resolve_proxy,
)
from paths import (  # noqa: E402
    ACCOUNTS_FILE,
    LOG_DIR,
    PROFILE_DIR,
    SESSION_FILE,
    ensure_layout,
)
from platform_compat import (  # noqa: E402
    browser_args,
    browser_launch_attempts,
    browser_missing_hint,
    browser_not_installed,
    has_display,
    headed_login_hint,
    os_label,
    private_file,
)
from settings import normalize_tag_query  # noqa: E402
from redact import redact  # noqa: E402
from user_agents import (  # noqa: E402
    impersonate_for,
    is_mobile,
    random_browser_user_agent,
    random_user_agent,
    request_headers,
)

WEB_APP_ID = 7879029  # VK Web (vk.ru). Токен = права вошедшего пользователя.
MOBILE_APP_ID = 6287487  # VK мессенджер (фолбэк для web_token)
API_VERSION = "5.289"

TOKEN_ENDPOINTS = [
    ("https://login.vk.ru/?act=web_token", "https://vk.ru"),
    ("https://login.vk.com/?act=web_token", "https://vk.com"),
]
API_BASES = [
    "https://api.vk.ru/method",
    "https://api.vk.com/method",
    "https://web.api.vk.ru/method",
]
# --- VK ID: актуальная разметка входа vk.ru (компоненты vkc__*) ---
# Реальный вход vk.ru идёт через VK ID, и шаги такие:
#   1) старт vk.ru/?u=2&to=… → ввод телефона → button[data-test-id="submit_btn"];
#   2) капча «я не робот» → человек жмёт label с not-robot-captcha-checkbox;
#   3) страница подтверждения (data-page="MODULE_VERIFICATION", data-test-id="otp-title"):
#      код (SMS/MAX) НЕ ждём — жмём «Войти при помощи пароля»
#      (button.vkc__Bottom-module__switchToPassword) или «Подтвердить другим способом»
#      (button[data-test-id="other-verification-methods"]);
#   4) страница пароля (EnterPasswordNoUserInfo) → input[name="password"] + «Продолжить».
# У части аккаунтов шаг 3 пропускается: после ввода номера сразу открывается пароль.
VKID_START_URL = "https://vk.ru/?u=2&to=L2luZGV4LnBocA--"
VKID_SUBMIT_BTN = 'button[data-test-id="submit_btn"]'
VKID_CAPTCHA_HINTS = [
    'input[data-test-id="not-robot-captcha-checkbox-input"]',
    "#not-robot-captcha-checkbox",
    'label[for="not-robot-captcha-checkbox"]',
]
# Капчу решает человек: кликабельна именно label, а не скрытый input.
VKID_CAPTCHA_LABEL = (
    'label[for="not-robot-captcha-checkbox"], '
    'label:has(input[data-test-id="not-robot-captcha-checkbox-input"])'
)
VKID_VERIFICATION_HINTS = [
    '#root [data-page="MODULE_VERIFICATION"]',
    '[data-page="MODULE_VERIFICATION"]',
    '[data-test-id="otp-title"]',
]
VKID_OTHER_METHODS_BTN = 'button[data-test-id="other-verification-methods"]'
VKID_SWITCH_TO_PASSWORD_BTN = "button.vkc__Bottom-module__switchToPassword"
VKID_PASSWORD_HINTS = [
    "form.vkc__EnterPasswordNoUserInfo-module__content",
    'input[name="password"][type="password"]',
]

LOGIN_START_URLS = [
    VKID_START_URL,
    "https://vk.ru/login",
    "https://vk.com/login",
]
# Лента: её перезагрузка (F5) в профиле браузера — самый надёжный способ оживить
# сессию. Заход в ленту живой сессией заставляет VK перевыпустить cookies, а по ним
# уже берётся новый access_token. Чистый HTTP-обмен теми же cookies VK часто режет
# («too much tries»), а заход в ленту из браузера — обычное поведение человека.
FEED_RELOAD_URLS = ["https://m.vk.ru/feed", "https://m.vk.com/feed"]

# UA окна входа (--login) — единственное место с настольным UA: это широкая форма
# для человека (капча, код, QR), а не запросы прогона. Все запросы к VK и окна ленты
# ходят телефонами: см. user_agents.py.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PHONE_SELECTORS = [
    'input[name="login"]',
    "input#index_email",
    'input[type="tel"]',
    'input[name="phone"]',
    'input[autocomplete="tel"]',
    'input[placeholder*="елефон"]',
    'input[placeholder*="омер"]',
    'input[inputmode="tel"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[placeholder*="ароль"]',
]
SUBMIT_SELECTORS = [
    VKID_SUBMIT_BTN,  # VK ID: кнопка «Войти»/«Продолжить»
    'button[type="submit"]',
    'button:has-text("Продолжить")',
    'button:has-text("Войти")',
    'button:has-text("Continue")',
    "[data-testid='button-primary']",
]
CAPTCHA_HINTS = [
    'iframe[src*="captcha"]',
    'img[src*="captcha"]',
    "#captcha",
    'input[name="captcha"]',
    "[class*='aptchaImage']",
    "[class*='CaptchaBox']",
]
OTP_HINTS = [
    'input[name="otp-cell"]',
    'input[autocomplete="one-time-code"]',
    "[class*='code-input']",
]
QR_HINTS = [
    'img[src^="data:image"]',
    "[class*='Qr']",
    "[class*='qr']",
]
COOKIE_HINTS = ("remixsid", "remixlang", "remixuser")
RESTORE_MAX_RETURNS = 3  # сколько раз возвращаться со «Восстановления доступа»
LOGIN_PAGE_ATTEMPTS = 3  # попытки открыть страницу входа (прокси бывает медленным)
LOGIN_PAGE_TIMEOUT_MS = 22500  # максимум на одну попытку открыть страницу входа
LOGIN_PAGE_MIN_TIMEOUT_MS = 7500  # минимум даже при маленьком --timeout
BLANK_PAGE_SEC = 10  # столько секунд пустое окно (about:blank) ждём до переоткрытия
BLANK_PAGE_RELOADS = 3  # сколько раз переоткрывать опустевшее окно входа
# Окно браузера на виду (show_browser / headless=False): после F5 даём человеку
# посмотреть на ленту, а не закрываем окно мгновенно.
FEED_SHOW_PAUSE_SEC = 2.5
# Обход ленты идёт HTTP-запросами (newsfeed.search), поэтому в браузере его не
# видно. Окно на виду во время обхода открывает ту же страницу поиска по тегу,
# что и сам обход — m.vk.ru/search/statuses?q=тег (vk.ru → vk.com, как в
# FEED_RELOAD_URLS): видно, какое окно времени берётся прямо сейчас. См. FeedViewer.
# Хосты мобильные: окно теперь мобильное (см. view_mobile_context()), а desktop-
# адрес VK отдал бы в нём другую разметку (см. mobile_view_host).
FEED_HOSTS = ("m.vk.ru", "m.vk.com")
VIEW_LOAD_TIMEOUT_MS = 20000  # столько ждём одну страницу обхода
VIEW_COOKIE_RELOAD_SEC = 120  # как часто перечитывать cookies сессии в окно
VIEW_MAX_FAIL_LOG = 3  # больше не засоряем лог одной и той же ошибкой окна
VIEW_STOP_SEC = 6.0  # сколько ждём закрытия окна при остановке прогона
VIEW_OVERLAY_ID = "vk-feed-view"  # подпись окна: какое окно обхода показано
# Cookies сессии ставим доменом (".vk.ru", ".vk.com"), а не по url: VK смотрит
# remixsid/httoken/domain_sid и на поддоменах (login.vk.ru, api.vk.ru, id.vk.ru).
# Host-only cookies на vk.ru туда не доходят — окно показывает «ошибка сети», а
# после F5 уезжает на страницу входа (проверено живым браузером с сессией аккаунта).
VIEW_COOKIE_DOMAINS = (".vk.ru", ".vk.com")
# Так выглядит адрес, на который VK уводит окно, когда cookies не подошли.
VIEW_LOGIN_MARKS = ("login.vk.ru", "login.vk.com", "id.vk.ru", "/login", "act=login")
VIEW_LOGIN_NOTE = "\n(VK просит вход: окно показывает страницу входа — обход идёт запросами)"
# Окно обхода НЕ перезагружает выдачу — ни на каждом окне времени, ни изредка: F5
# только портит показ. Страница приходит за секунду, а посты в ней — только через
# 5–10 секунд (VK отдаёт их своими запросами, проверено живым браузером), так что
# свежая перезагрузка снова оставляет окно пустым, пока посты не подъехали. Поэтому
# страница живёт весь прогон, а окно времени показывается прокруткой: посты
# догружаются сами, как при листании. Открываем выдачу заново только там, где
# листать больше нечего: сменился тег (другая выдача) или страница уехала на вход /
# закрылась (прокрутка не вышла). См. FeedViewer._show.
VIEW_SCROLL_SHARE = 0.85  # за прокрутку — почти экран, как колесом мыши
# Листает окно неровно, как палец человека: прокрутка — каждые
# VIEW_SCROLL_MIN_SEC–VIEW_SCROLL_MAX_SEC секунды (пауза берётся рандомом, см.
# FeedViewer._scroll_pause). Ровный шаг по таймеру выдал бы робота, а лента при этом
# стояла бы: листание не прекращается и между окнами обхода — посты догружаются сами
# (см. FeedViewer._tick).
VIEW_SCROLL_MIN_SEC = 0.2
VIEW_SCROLL_MAX_SEC = 0.45
# Лента VK на телефоне — это одна колонка постов, которая листается пальцем: так VK
# отдаёт лёгкую мобильную выдачу, а посты в ней догружаются прокруткой, а не
# перезагрузкой. Поэтому и окно обхода, и заход в ленту через F5 открываются мобильным
# экраном: viewport 390×844 (обычный телефон), тач, плотность экрана телефона и
# телефонный user-agent. Иначе VK видит широкое окно с колесом мыши и отдаёт
# настольную выдачу. Вход (--login) остаётся широким: капчу, код и QR в нём разбирает
# человек, ему так удобнее.
VIEW_VIEWPORT = {"width": 390, "height": 844}  # обычный телефон, а не 1366×900
VIEW_DEVICE_SCALE = 3.0  # плотность экрана телефона


def view_mobile_context() -> dict[str, Any]:
    """Экран мобильного окна ленты: телефонный UA — новый на каждое окно.

    Каждое новое окно (заход в ленту через F5, окно обхода) берёт из пула другой
    телефон из user_agents.py: один и тот же UA на каждой перезагрузке страницы —
    признак робота, а VK отдаёт ленту телефонной (лёгкой) выдачей только телефону.

    UA здесь — только Chrome на Android (random_browser_user_agent): окно рисует
    Chromium, и строка iPhone обещала бы Safari/WebKit, которого внутри нет. VK
    сверяет UA с тем, что рассказывает сам браузер (navigator.userAgentData,
    navigator.platform, window.chrome), и такое расхождение — примета подмены.
    """
    return {
        "viewport": dict(VIEW_VIEWPORT),
        "is_mobile": True,  # VK видит телефон и отдаёт мобильную (лёгкую) ленту
        "has_touch": True,  # листание пальцем, как на телефоне
        "device_scale_factor": VIEW_DEVICE_SCALE,
        "user_agent": random_browser_user_agent(),
    }
VIEW_POSTS_NOTE = "\n(постов в ленте: {n})"  # в подписи видно, что посты догрузились
# В окне на виду VK грузит ещё и чужую рекламу/статистику (ad.mail.ru,
# stats.vk-portal.net, akashi.vk-portal.net и прочее из списка ниже): самой выдаче
# они не нужны, а в консоли окна оставляют 403/504/ошибки CORS и отнимают полосу у
# постов, которые VK отдаёт не сразу. Эти хосты окно не грузит — см. view_noise_url
# и FeedViewer._route_noise. Ошибки вида «stats.vk-portal.net … net::ERR_FAILED» в
# консоли окна — это и есть наш запрет, а не отказ VK: страница их просто просила.
VIEW_NOISE_HOSTS = (
    "ad.mail.ru",  # реклама Mail.ru (в окне — 504)
    "stats.vk-portal.net",  # статистика VK (в окне — 403)
    "akashi.vk-portal.net",  # A/B-конфиг VK: из m.vk.ru идёт с ошибкой CORS
    "r3-test.vk.ru",  # тестовый контур рекламы VK (в окне — ошибка CORS)
    "mincifry-cert.vk.ru",  # проверка сертификатов Минцифры (в окне — битый сертификат)
)


def log(msg: str) -> None:
    # В лог входа попадают cookie из ошибок Playwright и адрес прокси — чистим здесь,
    # чтобы строки не ушли в панель/файл как есть (см. src/redact.py).
    line = f"[{time.strftime('%H:%M:%S')}] {redact(msg)}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:  # noqa: BLE001
            pass


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if not digits:
        return (raw or "").strip()
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}" if digits else ""


def cookie_map(cookies: list[dict]) -> dict:
    out: dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or not value:
            continue
        domain = c.get("domain") or ""
        if "vk.ru" not in domain and "vk.com" not in domain:
            continue
        out[name] = value
    return out


# Хранилище сессии
def save_session(payload: dict, path: Path = SESSION_FILE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    private_file(path)  # Linux/macOS: сессия только для владельца (в ней токен)
    return path


def load_session(path: Path = SESSION_FILE) -> Optional[dict]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def session_token(data: Optional[dict]) -> str:
    return str((data or {}).get("access_token") or "").strip()


def token_alive(data: Optional[dict], slack: int = 300) -> bool:
    data = data or {}
    token = session_token(data)
    if not token:
        return False
    expires = float(data.get("token_expires_at") or 0)
    if expires <= 0:
        return True
    return expires - slack > time.time()


def cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def has_login_cookie(cookies: dict) -> bool:
    for name, value in (cookies or {}).items():
        if name.startswith(COOKIE_HINTS[0]) and len(str(value)) > 5:
            return True
    return False


# HTTP-часть: обмен cookies -> token, проверка токена
class VkAuthError(RuntimeError):
    pass


class VkApiError(RuntimeError):
    def __init__(self, code: int, message: str, payload: Optional[dict] = None):
        super().__init__(f"VK API error {code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


def _http_session(proxy: str = "", user_agent: str = ""):
    """HTTP-сессия для запросов к VK: стартовый UA — телефон (Android или iPhone).

    Этой сессией ходят и запросы прогона, и обмен cookies на токен, и проверка
    прокси: настольного UA здесь нет — только мобильные из user_agents.py. Если
    передан не телефонный user_agent (или пусто) — берём случайный из пула.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:  # pragma: no cover
        raise VkAuthError("нужен пакет curl_cffi: pip install curl_cffi") from exc
    ua = user_agent if is_mobile(user_agent) else random_user_agent()
    session = cffi_requests.Session(impersonate=impersonate_for(ua))
    session.headers.update(
        {
            **request_headers(ua),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )
    proxy = normalize_proxy(proxy)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def rotate_user_agent(session) -> str:
    """Меняет UA живой сессии на другой телефон и возвращает его.

    Каждый запрос страницы уходит новым телефоном — как в
    vk_client.VkFeedClient.call. Импресонацию (TLS/HTTP2) запрос задаёт сам через
    impersonate=..., а заголовки (UA + client hints) — request_headers(). Подсказки
    прошлого телефона убираем: Safari на iPhone их не шлёт, и «хвост» от Android
    выдал бы подмену.
    """
    previous = session.headers.get("User-Agent") or ""
    ua = random_user_agent(previous)
    for key, value in request_headers(ua).items():
        if value is None:
            session.headers.pop(key, None)
        else:
            session.headers[key] = value
    return ua


def request_web_token_http(
    cookies: dict,
    proxy: str = "",
    app_id: int = WEB_APP_ID,
    api_version: str = API_VERSION,
) -> dict:
    """Меняет cookies браузера на user access_token (тот же путь, что у сайта VK)."""
    session = _http_session(proxy)
    headers = {
        "Cookie": cookie_header(cookies),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    errors: list[str] = []
    for url, origin in TOKEN_ENDPOINTS:
        # Смена cookies на токен — тоже запрос страницы: новый телефон на попытку.
        user_agent = rotate_user_agent(session)
        try:
            resp = session.post(
                url,
                data={"version": "1", "app_id": str(app_id)},
                headers={
                    **headers,
                    **request_headers(user_agent),
                    "Origin": origin,
                    "Referer": origin + "/",
                },
                impersonate=impersonate_for(user_agent),
                timeout=15,
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            # Playwright в тексте ошибки печатает запрос целиком, вместе с `cookie:`.
            errors.append(f"{url}: {redact(str(exc))}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{url}: не JSON")
            continue
        if payload.get("type") != "okay":
            info = payload.get("error_info") or payload.get("error") or payload.get("type")
            errors.append(f"{url}: {info}")
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = str(data.get("access_token") or "").strip()
        if not token:
            errors.append(f"{url}: пустой access_token")
            continue
        expires_in = int(data.get("expires_in") or 0)
        if expires_in <= 0 and data.get("expires"):
            expires_in = max(60, int(data["expires"]) - int(time.time()))
        if expires_in <= 0:
            expires_in = 3600
        log(f"web_token получен (app_id={app_id}, url={url})")
        return {
            "access_token": token,
            "expires_in": expires_in,
            "user_id": int(data.get("user_id") or 0),
            "app_id": app_id,
            "api_version": api_version,
        }
    raise VkAuthError("web_token недоступен — cookies не приняты: " + "; ".join(errors))


def call_api(
    method: str,
    params: dict,
    token: str,
    proxy: str = "",
    api_version: str = API_VERSION,
    timeout: int = 30,
) -> Any:
    """POST на api.vk.*/method/<method>. Возвращает response или бросает ошибку."""
    session = _http_session(proxy)
    body = {k: v for k, v in params.items() if v is not None}
    body["access_token"] = token
    body["v"] = api_version
    last_error = ""
    for base in API_BASES:
        # Каждый запрос — с новым мобильным UA и импресонацией под него (см. user_agents).
        user_agent = rotate_user_agent(session)
        try:
            resp = session.post(
                f"{base}/{method}",
                data=body,
                timeout=timeout,
                headers=request_headers(user_agent),
                impersonate=impersonate_for(user_agent),
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = f"{base}: {exc}"
            continue
        if not isinstance(payload, dict):
            last_error = f"{base}: не JSON"
            continue
        if "error" in payload:
            err = payload["error"]
            raise VkApiError(
                int(err.get("error_code") or 0),
                str(err.get("error_msg") or "unknown"),
                err,
            )
        return payload.get("response", payload)
    raise VkAuthError(f"API недоступен ({last_error})")


def validate_token(token: str, proxy: str = "") -> dict:
    """Проверяет токен через users.get; возвращает профиль."""
    response = call_api(
        "users.get",
        {"fields": "screen_name,first_name,last_name"},
        token=token,
        proxy=proxy,
    )
    if not isinstance(response, list) or not response:
        raise VkAuthError("users.get вернул пустой ответ")
    return response[0]


def build_session_payload(
    token_data: dict,
    cookies: dict,
    login: str = "",
    proxy: str = "",
    profile: Optional[dict] = None,
    source: str = "browser",
) -> dict:
    payload = {
        "access_token": token_data.get("access_token"),
        "token_expires_at": int(time.time()) + int(token_data.get("expires_in") or 3600),
        "user_id": token_data.get("user_id") or (profile or {}).get("id") or 0,
        "app_id": token_data.get("app_id", WEB_APP_ID),
        "api_version": token_data.get("api_version", API_VERSION),
        "cookies": cookies,
        "login": login,
        "proxy": proxy or "",
        "source": source,
    }
    if profile:
        payload["user"] = {
            "id": profile.get("id"),
            "first_name": profile.get("first_name", ""),
            "last_name": profile.get("last_name", ""),
            "screen_name": profile.get("screen_name", ""),
        }
    return payload


# Браузерный вход
def _visible(page, selectors: list[str], timeout_ms: int = 1200):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _any_visible(page, selectors: list[str], timeout_ms: int = 800) -> bool:
    return _visible(page, selectors, timeout_ms) is not None


def _text_of(page, limit: int = 220) -> str:
    try:
        body = page.locator("body").inner_text(timeout=1250)
    except Exception:  # noqa: BLE001
        return ""
    body = re.sub(r"\s+", " ", body or "").strip()
    return body[:limit]


def detect_stage(page) -> str:
    """Что сейчас просит VK: капчу, код, QR, пароль."""
    # VK ID: капча «я не робот» (чекбокс) — её решает человек.
    if _any_visible(page, VKID_CAPTCHA_HINTS) or _any_visible(page, CAPTCHA_HINTS):
        return "captcha"
    # VK ID: страница подтверждения входа (MODULE_VERIFICATION / otp-title).
    if _any_visible(page, VKID_VERIFICATION_HINTS) or _any_visible(page, OTP_HINTS):
        return "otp"
    # VK ID: форма пароля — в т.ч. когда пароль спросили сразу после номера.
    if _any_visible(page, VKID_PASSWORD_HINTS) or _any_visible(page, PASSWORD_SELECTORS):
        return "password"
    text = _text_of(page, 400).lower()
    if "отсканируйте" in text or "qr-код" in text or "qr‑код" in text:
        return "qr"
    if "подозрительная попытка" in text or "пройти проверку" in text:
        return "verify"
    if "капч" in text or "не робот" in text:
        return "captcha"
    if "код из" in text or "код подтверждения" in text:
        return "otp"
    if not text or "загружается" in text:
        return "loading"
    return "unknown"


def _submit(page) -> str:
    loc = _visible(page, SUBMIT_SELECTORS, 1500)
    if loc is not None:
        try:
            text = (loc.inner_text(timeout=500) or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        try:
            loc.click()
            return text or "submit"
        except Exception:  # noqa: BLE001
            pass
    try:
        page.keyboard.press("Enter")
        return "Enter"
    except Exception:  # noqa: BLE001
        return ""


def _screenshot(page, name: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(LOG_DIR / name), full_page=False)
    except Exception:  # noqa: BLE001
        pass


def _click_qr_option(page) -> bool:
    for pattern in ("QR", "qr", "Отсканировать"):
        try:
            loc = page.get_by_text(pattern, exact=False).first
            if loc.count() and loc.is_visible(timeout=500):
                loc.click()
                log("выбран вход по QR-коду — отсканируйте код в приложении VK")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_text(page, patterns) -> bool:
    for pattern in patterns:
        try:
            loc = page.get_by_text(pattern, exact=False).first
            if loc.count() and loc.is_visible(timeout=500):
                loc.click(timeout=2000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


OTHER_METHOD_TEXTS = (
    "Войти по паролю",
    "Войти с паролем",
    "Пароль",
    "Другие способы",
    "Другой способ",
    "Войти другим способом",
)


def _click_other_method(page) -> bool:
    """Уводит с QR-экрана на список способов / ввод пароля."""
    for pattern in OTHER_METHOD_TEXTS:
        try:
            loc = page.get_by_text(pattern, exact=False).first
            if loc.count() and loc.is_visible(timeout=500):
                loc.click()
                log(f"нажал «{pattern}» — перехожу к вводу пароля")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _switch_verification_to_password(page) -> bool:
    """Со страницы подтверждения входа уводит на ввод пароля.

    Порядок: сначала прямая ссылка «Войти при помощи пароля»
    (button.vkc__Bottom-module__switchToPassword), затем кнопка
    «Подтвердить другим способом» (button[data-test-id="other-verification-methods"]),
    затем фолбэк по тексту. Код (SMS/MAX) при этом не запрашивается.
    """
    for sel, label in (
        (VKID_SWITCH_TO_PASSWORD_BTN, "Войти при помощи пароля"),
        (VKID_OTHER_METHODS_BTN, "Подтвердить другим способом"),
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=600):
                loc.click(timeout=2000)
                log(f"страница подтверждения: нажал «{label}» — перехожу к вводу пароля")
                return True
        except Exception:  # noqa: BLE001
            continue
    return _click_text(
        page,
        (
            "Войти при помощи пароля",
            "Войти по паролю",
            "Войти с паролем",
            "Подтвердить другим способом",
            "Другие способы",
            "Другой способ",
        ),
    )


# Страницы VK без формы входа (маркетинг/FAQ): там кнопку «Пароль» не ищем.
NO_AUTH_URL_HINTS = ("/about", "/help", "/support", "/faq", "/dev", "/blog", "/terms")


def _is_auth_page(page) -> bool:
    """Можно ли искать на этой странице кнопку «Пароль»/«Другие способы».

    id.vk.ru уводит на маркетинговую /about/id, где в FAQ есть слова «пароль» и
    «Вход»: без этой проверки скрипт жмёт не туда и стоит на месте. Карточка входа
    бывает и на корне (vk.ru с QR-кодом), поэтому по умолчанию страница считается
    подходящей.
    """
    url = (page.url or "").lower()
    return not any(hint in url for hint in NO_AUTH_URL_HINTS)


def _field_digits(field) -> str:
    """Только цифры из значения поля ввода (телефон VK маскирует пробелами)."""
    try:
        value = field.input_value(timeout=500) or ""
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"\D+", "", value)


POPUP_SELECTORS = (
    "div[class*='vkuiPopoutWrapper__masked']",
    "div[class*='vkuiAlert']",
    "div[role='alertdialog']",
)
POPUP_CLOSE_TEXTS = ("Закрыть", "ОК", "Понятно", "Отмена", "Хорошо")
FLOOD_HINTS = ("flood control", "too many requests", "попробуйте позже", "слишком много")


def _read_popup(page) -> str:
    """Текст всплывающей ошибки VK (например, flood control)."""
    for sel in POPUP_SELECTORS:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible(timeout=250):
                continue
            text = re.sub(r"\s+", " ", (loc.inner_text(timeout=750) or "")).strip()
            if text:
                return text[:160]
        except Exception:  # noqa: BLE001
            continue
    return ""


def _dismiss_popup(page) -> None:
    """Закрывает модальное окно VK, чтобы оно не перехватывало клики."""
    for pattern in POPUP_CLOSE_TEXTS:
        try:
            loc = page.get_by_text(pattern, exact=False).first
            if loc.count() and loc.is_visible(timeout=300):
                loc.click(timeout=1500)
                return
        except Exception:  # noqa: BLE001
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


def _type_phone(page, field, login: str) -> bool:
    """Печатает номер по символам (маска VK сам добавляет +7)."""
    digits = re.sub(r"\D+", "", login)
    local = digits[1:] if len(digits) == 11 and digits.startswith("7") else digits
    try:
        field.click(timeout=1500)
    except Exception:  # noqa: BLE001
        try:
            field.focus(timeout=1500)
        except Exception:  # noqa: BLE001
            return False
    # Очистка: fill("") работает на любой ОС; на macOS Control+A не выделяет
    # текст (нужен Cmd), из-за чего старые цифры смешивались с новыми.
    for _ in range(3):
        try:
            field.fill("", timeout=1500)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(local, delay=60)
        except Exception:  # noqa: BLE001
            pass
        typed = _field_digits(field)
        # маска VK может показывать «7» в поле — сверяем по хвосту номера
        if typed.endswith(local) and len(typed) <= len(local) + 1:
            return True
        time.sleep(0.25)
    try:
        field.fill(local, timeout=2000)
        typed = _field_digits(field)
        return typed.endswith(local) and len(typed) <= len(local) + 1
    except Exception:  # noqa: BLE001
        return False


def _has_phone_field(page) -> bool:
    return _visible(page, PHONE_SELECTORS, 400) is not None


def _otp_input_empty(page) -> bool:
    """True, если поле кода пустое (значит человек его сейчас не заполняет)."""
    loc = _visible(page, OTP_HINTS, 500)
    if loc is None:
        return True
    try:
        return not (loc.input_value(timeout=400) or "").strip()
    except Exception:  # noqa: BLE001
        return True


def _wait_submit_enabled(page, seconds: float = 10.0) -> bool:
    """Кнопка «Продолжить» активна только когда VK принял номер."""
    deadline = time.time() + max(1.0, seconds)
    while time.time() < deadline:
        loc = _visible(page, SUBMIT_SELECTORS, 700)
        if loc is not None:
            try:
                if loc.is_enabled():
                    return True
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.25)
    return False


def _controls_dump(page, limit: int = 14) -> str:
    """Текст видимых кнопок/ссылок/полей — чтобы понимать, что предлагает VK."""
    seen: list[str] = []
    for sel in ("button", "a", "input", "[role='button']"):
        try:
            elements = page.locator(sel)
            count = min(elements.count(), 25)
        except Exception:  # noqa: BLE001
            continue
        for idx in range(count):
            try:
                el = elements.nth(idx)
                if not el.is_visible():
                    continue
                text = (
                    (el.inner_text() or "").strip()
                    or (el.get_attribute("placeholder") or "")
                    or (el.get_attribute("value") or "")
                    or (el.get_attribute("type") or "")
                )
                text = re.sub(r"\s+", " ", text)[:40]
                if not text:
                    continue
                tag = "input" if sel == "input" else sel
                item = f"{tag}:{text}"
                if item not in seen:
                    seen.append(item)
            except Exception:  # noqa: BLE001
                continue
    return " | ".join(seen[:limit])


def _stage_hint(stage: str) -> str:
    if stage == "verify":
        return (
            ">>> VK просит подтвердить вход («Пройти проверку») — жмите эту кнопку "
            "в окне, затем решите капчу"
        )
    if stage == "captcha":
        return (
            ">>> НУЖНА КАПЧА: решите её в открытом окне браузера "
            "— скрипт ждёт и продолжит сам"
        )
    if stage == "otp":
        return (
            ">>> VK просит код подтверждения — SMS не ждём: скрипт сам жмёт "
            "«Войти при помощи пароля». Если код уже пришёл — введите его в окне"
        )
    if stage == "qr":
        return (
            ">>> QR: можно отсканировать код приложением VK («Сканировать QR»), "
            "иначе через 25 с скрипт сам перейдёт к вводу пароля"
        )
    return ""


def _launch_context(pw, profile_dir: Path, launch: dict):
    """Открывает браузер входа.

    Пробуем по очереди всё, что есть в системе (порядок — в platform_compat):
    Windows — Edge, затем Chrome (во встроенный Chromium exe-сборки он не попадает,
    поэтому раньше был WinError 2); Linux — встроенный Chromium от
    `playwright install chromium`, затем системные Chrome/Chromium по имени и по
    пути. Такой же перебор делает вход рабочим и на сервере.
    """
    last_error: Exception | None = None
    for attempt in browser_launch_attempts():
        name = attempt.get("channel") or attempt.get("executable_path") or "chromium (встроенный)"
        try:
            return pw.chromium.launch_persistent_context(str(profile_dir), **attempt, **launch)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if browser_not_installed(exc):
                # Такого браузера в системе просто нет — это не ошибка, а запасной путь.
                log(f"браузер {name} не установлен — пробую следующий")
            else:
                log(f"браузер {name} не запустился: {str(exc).splitlines()[0][:160]}")
    raise VkAuthError(browser_missing_hint(last_error))


def _nav_timeout_ms(timeout_sec: int) -> int:
    """Сколько ждать одну попытку открыть страницу входа.

    Не больше трети общего --timeout: если прокси молчит, лучше попробовать
    ещё раз, чем простоять весь срок на одном мёртвом соединении.
    """
    budget = max(LOGIN_PAGE_MIN_TIMEOUT_MS, int(timeout_sec) * 1000 // 3)
    return min(LOGIN_PAGE_TIMEOUT_MS, budget)


def _page_is_blank(page) -> bool:
    """True, если окно не показало страницу: about:blank, chrome-error://, data:."""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        return True  # окно закрыто или недоступно
    return not url.startswith("http")


def _goto_login(page, url: str, timeout_ms: int) -> tuple[bool, str]:
    """Открывает страницу входа и не падает, если прокси молчит.

    С мёртвым прокси Playwright кидает «net::ERR_PROXY_CONNECTION_FAILED», а с
    медленным — «Timeout … waiting until "domcontentloaded"», и окно при этом
    остаётся на about:blank. Поэтому ошибку не выбрасываем наружу: сначала ждём
    domcontentloaded, затем commit (срабатывает, даже когда документ не догрузился),
    а результат возвращаем — (открылась ли страница, причина последней ошибки).
    """
    last_error = ""
    for wait in ("domcontentloaded", "commit"):
        try:
            page.goto(url, wait_until=wait, timeout=timeout_ms)
            if not _page_is_blank(page):
                return True, ""
            reason = f"{wait}: окно осталось на about:blank"
        except Exception as exc:  # noqa: BLE001
            reason = f"{wait}: {str(exc).splitlines()[0][:160]}"
            if not _page_is_blank(page):
                return True, ""  # VK увёл страницу на другой адрес — она открыта
        last_error = f"{last_error} | {reason}" if last_error else reason
        try:
            page.stop()  # брошенная загрузка не мешает следующей попытке
        except Exception:  # noqa: BLE001
            pass
    return False, last_error


def _open_login_page(
    page, url: str, timeout_ms: int, attempts: int = LOGIN_PAGE_ATTEMPTS
) -> tuple[bool, str]:
    """Пробует открыть страницу входа несколько раз и пишет в лог, что мешает."""
    error = ""
    for attempt in range(1, attempts + 1):
        ok, error = _goto_login(page, url, timeout_ms)
        if ok:
            if attempt > 1:
                log(f"страница входа открылась с попытки {attempt}: {(page.url or '')[:90]}")
            return True, ""
        log(
            f"страница входа не открылась (попытка {attempt}/{attempts}): "
            f"{error or 'окно осталось на about:blank'}"
        )
        if attempt < attempts:
            time.sleep(1.5)
    return False, error


def _press_f5(page, timeout_ms: int, errors: list[str], url: str) -> str:
    """Жмёт F5 в открытой ленте: сначала перезагрузка движком, затем клавиша.

    Возвращает, чем именно обновили страницу ("" — не вышло). Причину пишем в errors,
    чтобы вызывающий мог сказать в логе, что помешало.
    """
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)  # F5 движком
        return "reload"
    except Exception as exc:  # noqa: BLE001 — движок мог не принять перезагрузку
        errors.append(f"{url}: reload: {str(exc).splitlines()[0][:120]}")
    try:
        page.keyboard.press("F5")  # настоящее нажатие F5
        return "клавиша F5"
    except Exception as exc:  # noqa: BLE001 — окно закрыли или движок уже мёртв
        errors.append(f"{url}: клавиша F5: {str(exc).splitlines()[0][:120]}")
    return ""


def _reload_feed(page, timeout_ms: int = 30000) -> tuple[bool, str]:
    """Открывает ленту VK и жмёт в ней F5 — «обновление ленты» в самом браузере.

    Так сессия обновляется через эмулятор браузера, а не через HTTP-обмен: браузер
    заходит в ленту сохранённым профилем, VK видит живую сессию и выдаёт свежие
    cookies (remixsid и остальные), а по ним уже берётся новый access_token. Именно
    поэтому путь проходит, когда login.vk.ru/?act=web_token отвечает «too much
    tries»: для VK это обычный заход в ленту, а не подозрительный обмен токена.

    Возвращает (получилось ли открыть и обновить ленту, причина последней ошибки).
    """
    errors: list[str] = []
    for url in FEED_RELOAD_URLS:
        opened, nav_error = _goto_login(page, url, timeout_ms)
        if not opened:
            errors.append(f"{url}: {nav_error or 'окно осталось на about:blank'}")
            continue
        pressed = _press_f5(page, timeout_ms, errors, url)
        try:
            # Ждём отрисовку: свежие cookies VK отдаёт уже на загрузке документа,
            # а часть ресурсов ленты может не догрузиться — это не помеха.
            page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 15000))
        except Exception:  # noqa: BLE001
            pass
        if pressed and not _page_is_blank(page):
            log(f"лента обновлена через {pressed}: {(page.url or '')[:70]}")
            return True, ""
    return False, " | ".join(errors)


def _follow_login_page(context, page):
    """Если вход идёт в другом окне, а это осталось пустым — переключаемся на VK."""
    if not _page_is_blank(page):
        return page
    try:
        pages = list(context.pages)
    except Exception:  # noqa: BLE001
        return page  # браузер уже закрыт
    for candidate in reversed(pages):
        if candidate is page:
            continue
        try:
            if candidate.is_closed() or _page_is_blank(candidate):
                continue
            url = candidate.url or ""
        except Exception:  # noqa: BLE001
            continue
        log(f"вход идёт в другом окне VK — переключаюсь: {url[:90]}")
        return candidate
    return page


def login_interactive(
    account: Optional[Account] = None,
    proxy_raw: str = "",
    *,
    timeout: int = 900,
    start_url: str = "",
    use_qr: bool = False,
    autofill: bool = True,
    headless: bool = False,
    password_only: bool = False,
    profile_dir: Optional[Path] = None,
) -> dict:
    """Открывает Chromium, входит в VK и сохраняет сессию (+user token).

    Капчу/код/QR завершает человек в открытом окне: скрипт только подставляет
    логин с паролем и ждёт появления remixsid (до timeout секунд).

    password_only — вход только номером и паролем: скрипт не запрашивает коды
    (SMS/MAX) и не нажимает «Пройти проверку», а если VK увёл в «Восстановление
    доступа» (типично при блокировке IP) — возвращается на страницу входа,
    ничего не нажимая в restore.
    """
    # Видимый вход требует графической сессии: на сервере без X сразу говорим,
    # что делать (проверяем до импорта playwright — он тут ни при чём).
    if not headless and not has_display():
        raise VkAuthError(f"{headed_login_hint()} (система: {os_label()})")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise VkAuthError("нужен playwright: pip install playwright") from exc

    ensure_layout()
    profile_dir = Path(profile_dir or PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)

    account = account or Account(login="", password="")
    login = normalize_phone(account.login) if account.login else ""
    proxy = normalize_proxy(proxy_raw or resolve_proxy(account))
    if not proxy:
        raise VkAuthError(
            "у аккаунта нет своего прокси — вход без прокси запрещён. "
            "Закрепите прокси в accounts.txt или привяжите запас прокси"
        )
    # Видимый вход на сервере без X-сервера невозможен — проверено выше,
    # поэтому ошибка про графику появляется сразу, а не как «браузер не запустился».
    launch: dict[str, Any] = {
        "headless": headless,
        "locale": "ru-RU",
        "viewport": {"width": 1366, "height": 900},
        "user_agent": BROWSER_UA,
        "timezone_id": "Europe/Moscow",
        "args": browser_args(),
    }
    pw_proxy = proxy_to_playwright(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    os.environ["VK_AUTH_PROXY"] = proxy
    log(f"профиль браузера: {profile_dir}")
    log(f"прокси: {proxy_host_port(proxy) or '— (напрямую)'}")
    log(f"аккаунт: {login or '— (вход вручную)'}")
    if password_only:
        log(
            "режим --password-only: подставляю только номер и пароль, "
            "коды (SMS/MAX) не запрашиваю, «Пройти проверку» жмёт человек"
        )

    token_data: dict = {}
    cookies: dict = {}
    last_stage = ""
    with sync_playwright() as pw:
        context = _launch_context(pw, profile_dir, launch)
        try:
            context.set_default_timeout(15000)
            page = context.pages[0] if context.pages else context.new_page()
            login_url = start_url or LOGIN_START_URLS[0]
            # Профиль уже вошёл (обновление токена) — ждать загрузку страницы не нужно:
            # remixsid виден в cookies сразу, а сеть может быть недоступна.
            try:
                already_logged = has_login_cookie(cookie_map(context.cookies()))
            except Exception:  # noqa: BLE001
                already_logged = False
            opened, page_error = _open_login_page(
                page,
                login_url,
                _nav_timeout_ms(timeout),
                attempts=1 if already_logged else LOGIN_PAGE_ATTEMPTS,
            )
            if not opened and not already_logged:
                _screenshot(page, "vk_login_blank.png")
                raise VkAuthError(
                    "страница входа VK не открылась: окно осталось на about:blank. "
                    f"Обычно это прокси аккаунта (прокси: {proxy_host_port(proxy) or '—'}): "
                    "соединения нет "
                    "или туннель обрывается — проверьте прокси и повторите вход. "
                    f"Последняя ошибка: {redact(str(page_error or 'окно пустое'))}"
                )
            if not opened:
                log("профиль уже вошёл — страница входа не понадобилась")

            phone_attempts = 0
            password_attempts = 0
            other_clicks = 0
            switch_clicks = 0
            restore_hits = 0
            verify_clicked = False
            sms_requested = False
            otp_since = time.time()
            stage_since = time.time()
            last_submit = 0.0
            last_phone_fill = 0.0
            last_password_submit = 0.0
            cooldown_until = 0.0
            last_beat = 0.0
            blank_since = time.time()
            blank_reloads = 0
            deadline = time.time() + max(60, timeout)

            while time.time() < deadline:
                try:
                    cookies = cookie_map(context.cookies())
                except Exception as exc:  # noqa: BLE001
                    raise VkAuthError(
                        "браузер закрылся во время входа "
                        f"({str(exc).splitlines()[0][:120]})"
                    ) from exc
                if has_login_cookie(cookies):
                    log("вход выполнен: получен remixsid")
                    break

                if page.is_closed():
                    # человек мог закрыть пустое окно, а вход идёт в другом
                    page = _follow_login_page(context, page)
                    if page.is_closed():
                        raise VkAuthError("окно браузера закрыто — вход прерван")
                if _page_is_blank(page):
                    page = _follow_login_page(context, page)
                if _page_is_blank(page):
                    # окно пустое: обычно отвалился прокси — переоткрываем вход
                    if (
                        blank_reloads < BLANK_PAGE_RELOADS
                        and time.time() - blank_since > BLANK_PAGE_SEC
                    ):
                        blank_reloads += 1
                        log(
                            f"окно VK осталось на about:blank ({BLANK_PAGE_SEC} с) — "
                            f"открываю страницу входа заново "
                            f"(попытка {blank_reloads}/{BLANK_PAGE_RELOADS})"
                        )
                        ok, nav_error = _goto_login(page, login_url, _nav_timeout_ms(timeout))
                        if not ok:
                            log(f"страница входа не открылась заново: {nav_error}")
                        blank_since = time.time()
                    elif blank_reloads >= BLANK_PAGE_RELOADS:
                        _screenshot(page, "vk_login_blank.png")
                        raise VkAuthError(
                            "окно VK так и осталось на about:blank: прокси "
                            f"{proxy_host_port(proxy) or '—'} не открывает страницу входа. "
                            "Смените прокси аккаунта и повторите вход"
                        )
                    if _page_is_blank(page):
                        time.sleep(1.5)
                        continue
                else:
                    blank_since = time.time()

                in_restore = "/restore" in (page.url or "")
                if in_restore and restore_hits < RESTORE_MAX_RETURNS:
                    restore_hits += 1
                    log(
                        "VK увёл в «Восстановление доступа» — возвращаюсь на страницу "
                        f"входа (попытка {restore_hits}/{RESTORE_MAX_RETURNS}), "
                        "в restore ничего не нажимаю"
                    )
                    ok, nav_error = _goto_login(
                        page, login_url, _nav_timeout_ms(timeout)
                    )
                    if not ok or "/restore" in (page.url or ""):
                        log(
                            "не удалось вернуться на страницу входа: "
                            f"{nav_error or (page.url or '')[:90]}"
                        )
                    last_stage = ""
                    stage_since = time.time()
                    time.sleep(1)
                    continue

                popup = _read_popup(page)
                if popup:
                    low = popup.lower()
                    if any(hint in low for hint in FLOOD_HINTS):
                        cooldown_until = max(cooldown_until, time.time() + 60)
                        log(f"VK: {popup} → пауза 120 с (повтор? смените прокси)")
                    else:
                        log(f"VK показал окно: {popup}")
                    _dismiss_popup(page)
                    last_beat = time.time()
                    _screenshot(page, "vk_login_popup.png")
                    time.sleep(1)
                    continue

                if time.time() < cooldown_until:
                    time.sleep(2.5)
                    continue

                stage = detect_stage(page)
                if stage != last_stage:
                    log(f"экран VK: {stage}  url={page.url[:90]}")
                    log(f"   доступно: {_controls_dump(page)}")
                    hint = _stage_hint(stage)
                    if hint:
                        log(hint)
                    _screenshot(page, f"vk_login_{stage}.png")
                    last_stage = stage
                    stage_since = time.time()

                if (
                    autofill
                    and login
                    and phone_attempts < 3
                    and stage != "loading"
                    and not in_restore
                    and time.time() - last_phone_fill > 6
                ):
                    field = _visible(page, PHONE_SELECTORS, 1500)
                    if field is not None:
                        wanted = re.sub(r"\D+", "", login)
                        current = _field_digits(field)
                        if not current.endswith(wanted[-10:]):
                            ok = _type_phone(page, field, login)
                            phone_attempts += 1
                            last_phone_fill = time.time()
                            if ok:
                                log(
                                    f"телефон введён ({phone_attempts}): "
                                    f"{_field_digits(field) or '—'}"
                                )
                                if _wait_submit_enabled(page, 8):
                                    pressed = _submit(page)
                                    log(f"нажато: {pressed}")
                                else:
                                    log("кнопка не активна — номер VK не принял")
                            else:
                                log(f"не удалось ввести телефон ({phone_attempts})")
                        elif phone_attempts and time.time() - last_submit > 5:
                            last_submit = time.time()
                            pressed = _submit(page)
                            log(f"номер уже в поле, повторяю отправку: {pressed}")

                if stage == "otp":
                    # Страница подтверждения входа (MODULE_VERIFICATION / otp-title).
                    # Код (SMS/MAX) НЕ ждём: уводим на вход по паролю —
                    # «Войти при помощи пароля» / «Подтвердить другим способом».
                    if password_only and not sms_requested:
                        sms_requested = True
                        log(
                            "VK просит код, режим --password-only: код не запрашиваю, "
                            "перехожу к вводу пароля"
                        )
                    if (
                        autofill
                        and not in_restore
                        and switch_clicks < 4
                        and time.time() - otp_since > 3
                        and _otp_input_empty(page)
                    ):
                        log(
                            "страница подтверждения входа — не жду код, "
                            "переключаюсь на «Войти при помощи пароля»"
                        )
                        if _switch_verification_to_password(page):
                            switch_clicks += 1
                            otp_since = time.time()
                else:
                    otp_since = time.time()
                    sms_requested = False

                if stage == "verify" and not verify_clicked:
                    verify_clicked = True
                    if password_only:
                        log(
                            "экран «подозрительный вход» — нажмите «Пройти проверку» "
                            "в окне сами (в режиме --password-only автоклик отключён)"
                        )
                    else:
                        clicked = _click_text(page, ("Пройти проверку", "Подтвердить"))
                        log(f"экран «подозрительный вход»: нажато «Пройти проверку» = {clicked}")

                if (
                    autofill
                    and (account.password or "")
                    and password_attempts < 3
                    and stage == "password"
                    and not in_restore
                    and time.time() - last_password_submit > 8
                ):
                    field = _visible(page, PASSWORD_SELECTORS, 1500)
                    if field is not None:
                        try:
                            field.click(timeout=1500)
                            field.fill(account.password or "", timeout=2000)
                        except Exception as exc:  # noqa: BLE001
                            log(f"не удалось подставить пароль: {exc}")
                        password_attempts += 1
                        last_password_submit = time.time()
                        pressed = _submit(page)
                        log(f"пароль подставлен ({password_attempts}), нажато: {pressed}")

                if (
                    not use_qr
                    and autofill
                    and login
                    and (
                        stage in ("qr", "otp")
                        or (stage == "unknown" and not _has_phone_field(page))
                    )
                    and other_clicks < 3
                    and not in_restore
                    and _is_auth_page(page)
                    and time.time() - stage_since > 25
                    and _otp_input_empty(page)
                ):
                    log(
                        f"экран «{stage}» без действий 25 с — переключаюсь на пароль: "
                        f"{_controls_dump(page)}"
                    )
                    if _click_other_method(page):
                        other_clicks += 1
                        stage_since = time.time()

                now = time.time()
                if now - last_beat >= 20:
                    last_beat = now
                    log(
                        f"…ожидание входа (осталось {int(deadline - now)} с): "
                        f"stage={stage}, url={page.url[:70]}, "
                        f"текст={_text_of(page, 110)!r}"
                    )
                    _screenshot(page, "vk_login_last.png")
                time.sleep(1.5)

            cookies = cookie_map(context.cookies())
            if not has_login_cookie(cookies):
                _screenshot(page, "vk_login_timeout.png")
                raise VkAuthError(
                    f"вход не завершён за {timeout} с (последний экран: {last_stage}). "
                    f"Скриншот: {LOG_DIR / 'vk_login_timeout.png'}"
                )

            # Обновляем cookies после редиректа входа: заходим в ленту и жмём F5. Если
            # лента не открылась — это не беда: remixsid уже есть, ниже сразу идёт
            # обмен на web_token.
            feed_ok, feed_error = _reload_feed(page, 30000)
            if not feed_ok:
                log(f"ленту обновить не удалось ({feed_error}) — беру cookies как есть")
            cookies = cookie_map(context.cookies())

            log("вымениваю web_token из cookies браузера…")
            token_data = _web_token_from_browser(context)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    return finish_login(token_data, cookies, login, proxy, account, profile_dir=profile_dir)


def finish_login(
    token_data: dict,
    cookies: dict,
    login: str,
    proxy: str,
    account: Optional[Account] = None,
    profile_dir: Optional[Path] = None,
) -> dict:
    """Проверяет токен, собирает и сохраняет сессию (data/session*.json)."""
    profile: dict = {}
    try:
        profile = validate_token(token_data["access_token"], proxy)
        log(
            f"токен рабочий: {profile.get('first_name', '')} "
            f"{profile.get('last_name', '')} (id {profile.get('id')})"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"предупреждение: не удалось проверить токен ({exc})")

    payload = build_session_payload(
        token_data, cookies, login=login, proxy=proxy, profile=profile, source="browser"
    )
    if profile_dir:
        # по этому профилю парсер сам обновит токен, когда он истечёт
        payload["profile_dir"] = str(Path(profile_dir).resolve())
    account = account or Account(login=login)
    save_session(payload, account.session_path)
    log(f"сессия сохранена: {account.session_path}")
    if account.session_path != SESSION_FILE:
        save_session(payload, SESSION_FILE)
        log(f"сессия сохранена: {SESSION_FILE}")
    # Статус в базе: раньше после входа там оставался старый «мёртв».
    try:
        from state_db import DB
        DB().mark_account(
            account.login or login,
            user_id=int(profile.get("id") or 0) or None,
            proxy=proxy or "",
            status="ready",
            error="",
            touch_ok=True,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"предупреждение: статус в базе не обновлён ({exc})")
    return payload


def _web_token_from_browser(context) -> dict:
    """Обмен cookies → user token: сначала сетевой стек Chromium, затем HTTP."""
    errors: list[str] = []
    for url, origin in TOKEN_ENDPOINTS:
        try:
            resp = context.request.post(
                url,
                form={"version": "1", "app_id": str(WEB_APP_ID)},
                headers={"Origin": origin, "Referer": origin + "/"},
                timeout=15000,
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            # В ошибке Chromium тоже бывают заголовки запроса с cookie сессии.
            errors.append(f"{url}: {redact(str(exc))}")
            continue
        if isinstance(payload, dict) and payload.get("type") == "okay":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            token = str(data.get("access_token") or "").strip()
            if token:
                log(f"web_token получен через браузер ({url})")
                return {
                    "access_token": token,
                    "expires_in": int(data.get("expires_in") or 0) or 3600,
                    "user_id": int(data.get("user_id") or 0),
                    "app_id": WEB_APP_ID,
                    "api_version": API_VERSION,
                }
        info = payload.get("error_info") if isinstance(payload, dict) else "не JSON"
        errors.append(f"{url}: {info}")

    log(f"браузерный web_token не сработал ({'; '.join(errors)}), пробую HTTP…")
    cookies = cookie_map(context.cookies())
    proxy = normalize_proxy(os.environ.get("VK_AUTH_PROXY", ""))
    return request_web_token_http(cookies, proxy=proxy)


def _save_refreshed_session(
    session_path: Path,
    old: dict,
    token_data: dict,
    cookies: dict,
    proxy: str,
    source: str,
) -> dict:
    """Сохраняет сессию после обновления токена, не теряя профиль браузера.

    Важно: profile_dir (и user) переносим из прежнего файла. Раньше обмен cookies
    перезаписывал сессию целиком, profile_dir пропадал — и обновление через профиль
    браузера/F5 больше не поднялось бы: аккаунт оставался только с HTTP-обменом,
    который VK как раз и режет («too much tries»).
    """
    profile: dict = {}
    try:
        profile = validate_token(token_data["access_token"], proxy)
        log(
            f"токен рабочий: {profile.get('first_name', '')} "
            f"{profile.get('last_name', '')} (id {profile.get('id')})"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"предупреждение: токен не подтвердился ({exc})")
    payload = build_session_payload(
        token_data,
        cookies,
        login=old.get("login", ""),
        proxy=proxy,
        profile=profile or old.get("user") or {},
        source=source,
    )
    if old.get("profile_dir"):
        payload["profile_dir"] = old["profile_dir"]
    save_session(payload, session_path)
    return payload


def token_via_feed_reload(
    session_path: Path,
    proxy_raw: str = "",
    timeout: int = 90,
    headless: bool = True,
) -> dict:
    """Свежий токен через профиль браузера и F5 на ленте — без участия человека.

    Открываем сохранённый профиль, заходим в ленту, жмём F5, забираем cookies,
    которые VK выдал живой сессии, и меняем их на access_token сетевым стеком самого
    Chromium. Для VK это обычный заход в ленту из браузера, поэтому путь проходит и
    когда HTTP-обмен (login.vk.ru/?act=web_token) отвечает «too much tries». Если
    профиль уже не вошёл (VK просит капчу/код) — VkAuthError, и вызывающий уходит на
    полный вход, а человек при необходимости входит через --login.

    headless=False — окно браузера на виду (настройка show_browser / флаг
    --show-browser в прогоне): видно, как эмулятор открывает ленту и жмёт F5, после
    чего окно держится FEED_SHOW_PAUSE_SEC секунд и закрывается. Требует графической
    сессии.
    """
    data = load_session(session_path) or {}
    profile_dir = data.get("profile_dir")
    if not profile_dir or not Path(profile_dir).exists():
        raise VkAuthError("в сессии нет профиля браузера — один раз войдите через --login --profile")
    if not headless and not has_display():
        raise VkAuthError(
            "показывать окно браузера не могу: графической сессии нет "
            f"({os_label()}) — на сервере выключите show_browser/--show-browser "
            "либо запускайте через xvfb-run"
        )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise VkAuthError("нужен playwright: pip install playwright") from exc

    proxy = normalize_proxy(proxy_raw or data.get("proxy") or "")
    os.environ["VK_AUTH_PROXY"] = proxy
    launch: dict[str, Any] = {
        "headless": bool(headless),
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
        "args": browser_args(),
        # Ленту открываем мобильным экраном (см. view_mobile_context()): VK отдаёт её
        # лёгкой телефонной выдачей. Тот же экран и у окна обхода (FeedViewer._open).
        **view_mobile_context(),
    }
    pw_proxy = proxy_to_playwright(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy
    log(f"профиль браузера (F5): {profile_dir}")
    log(f"прокси: {proxy_host_port(proxy) or '— (напрямую)'}")
    if not headless:
        log(
            "браузер открыт на виду: зайду в ленту и обновлю её через F5 — "
            "окно закроется само"
        )

    with sync_playwright() as pw:
        context = _launch_context(pw, Path(profile_dir), launch)
        try:
            context.set_default_timeout(15000)
            try:
                # В окне F5 — та же мобильная лента, и загрузчик VK так же пишет в
                # консоль «Not found blocking …» (см. VIEW_CONSOLE_MUTE_JS). Окно
                # показывается человеку, поэтому глушим этот шум и здесь.
                context.add_init_script(VIEW_CONSOLE_MUTE_JS)
            except Exception as exc:  # noqa: BLE001 — без глушения лента всё равно откроется
                log(f"браузер (F5): шум VK в консоли не заглушил ({exc})")
            page = context.pages[0] if context.pages else context.new_page()
            feed_ok, feed_error = _reload_feed(page, _nav_timeout_ms(max(60, timeout)))
            if not headless and feed_ok:
                time.sleep(FEED_SHOW_PAUSE_SEC)  # дать человеку увидеть саму ленту
            cookies = cookie_map(context.cookies())
            if not has_login_cookie(cookies):
                raise VkAuthError(
                    "в профиле нет remixsid — сессия кончилась, нужен вход --login "
                    f"({feed_error or 'лента не открылась'})"
                )
            if not feed_ok:
                log(f"лента не обновилась ({feed_error}), но remixsid на месте — беру cookies как есть")
            log("вымениваю web_token из cookies браузера…")
            token_data = _web_token_from_browser(context)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    return _save_refreshed_session(session_path, data, token_data, cookies, proxy, source="browser-f5")


def refresh_via_browser(
    session_path: Path, proxy_raw: str = "", timeout: int = 90, headless: bool = True
) -> dict:
    """Новый токен через сохранённый профиль браузера — без участия человека.

    Сначала лёгкий путь: открыть профиль, обновить ленту через F5 и забрать cookies,
    которые VK выдал живой сессии. Если профиль уже не вошёл (VK просит капчу/код),
    идём прежним путём — полный вход в профиль до timeout секунд; тогда нужен
    ручной вход --login.

    headless=False — окно браузера на виду: и F5 на ленте, и запасной полный вход
    идут в видимом окне (см. show_browser / --show-browser в прогоне).
    """
    try:
        payload = token_via_feed_reload(session_path, proxy_raw, timeout=timeout, headless=headless)
        log(f"токен обновлён через профиль браузера (F5): {session_path}")
        return payload
    except VkAuthError as exc:
        log(f"профиль+F5 не помог ({exc}) — пробую полный вход в профиль…")
    data = load_session(session_path) or {}
    profile_dir = data.get("profile_dir")
    if not profile_dir or not Path(profile_dir).exists():
        raise VkAuthError("в сессии нет профиля браузера — один раз войдите через --login --profile")
    account = Account(login=data.get("login") or "", session_file=Path(session_path))
    return login_interactive(
        account,
        proxy_raw or data.get("proxy") or "",
        timeout=timeout,
        autofill=False,
        headless=headless,
        profile_dir=Path(profile_dir),
    )


def mobile_view_host(host: str = "") -> str:
    """Мобильный адрес окна: «vk.ru» → «m.vk.ru», «vk.com» → «m.vk.com».

    Окно обхода ленты теперь мобильное (см. view_mobile_context()), 390×844,
    мобильный user-agent), поэтому и страницу ему надо давать мобильную: на
    desktop-адресе VK отдаёт другую разметку, и в окне видно не ту выдачу, что
    собирает обход. Список основных хостов — FEED_HOSTS; пустой хост — первый из
    них. Хост, который уже начинается с «m.», оставляем как есть.
    """
    value = (host or "").strip().lower().removeprefix("www.").rstrip("/")
    if not value:
        return FEED_HOSTS[0]
    if value.startswith("m."):
        return value
    return f"m.{value}"


def feed_search_url(tag: str, host: str = FEED_HOSTS[0], mode: str = "hash") -> str:
    """Страница VK с поиском по тегу — то же, что обходит прогон.

    Обход постов идёт HTTP-методом newsfeed.search, и увидеть его в браузере можно
    только открыв ту же выдачу глазами: m.vk.ru/search/statuses?q=#тег (мобильный
    адрес — см. mobile_view_host). Тег без решётки и без пробелов по краям; пустой
    тег — просто страница поиска, без запроса.

    mode — чем искать тег, как в настройке tag_query (см. vk_client.search_query):
    «hash» (по умолчанию, как было) — с решёткой, «word» — словом без неё. У VK это
    разные выдачи, поэтому окно обязано открывать ту же, что берёт прогон, иначе
    оно показывало бы не то, что собирается.
    """
    clean = (tag or "").strip().lstrip("#").strip()
    url = f"https://{mobile_view_host(host)}/search/statuses"
    if not clean:
        return url
    word = normalize_tag_query(mode) == "word"
    return f"{url}?q={quote(clean if word else f'#{clean}', safe='')}"


def view_cookie_jar(cookies: dict) -> list[dict]:
    """Cookies сессии так, как их поставил бы сам браузер — на домен .vk.ru/.vk.com.

    Host-only cookies (подстановка по url) VK видит только на самом vk.ru, а его
    собственные запросы идут на поддомены (login.vk.ru, api.vk.ru, id.vk.ru). Без
    remixsid/httoken они анонимные, и окно вместо ленты получает «ошибку сети», а
    после F5 — страницу входа. Домен-кука доходит и до поддомена, поэтому сессия в
    окне живая (проверено живым Chromium на сессии аккаунта).
    """
    return [
        {"name": str(name), "value": str(value), "domain": domain, "path": "/"}
        for name, value in (cookies or {}).items()
        for domain in VIEW_COOKIE_DOMAINS
        if name and value
    ]


def is_login_page_url(url: str) -> bool:
    """True — VK увёл окно на вход, значит cookies сессии окну не подошли."""
    low = (url or "").lower()
    return any(mark in low for mark in VIEW_LOGIN_MARKS)


def view_noise_url(url: str) -> bool:
    """True — запрос к чужой рекламе/статистике: окну на виду он не нужен.

    Окно показывает выдачу VK, а реклама и счётчики (ad.mail.ru, stats.vk-portal.net
    и прочее из VIEW_NOISE_HOSTS) ленте не нужны: в консоли окна они оставляют
    403/504/CORS, а посты VK отдаёт не сразу — лишние запросы только отнимают полосу.
    Правило это же окно ставит и браузеру (см. FeedViewer._route_noise).

    Хост сверяем целиком или по поддомену, чтобы чужое имя вида
    «ad.mail.ru.чужой-домен» под запрет не попало.
    """
    host = urlsplit(url or "").netloc.lower().rpartition("@")[2].partition(":")[0]
    return any(host == noise or host.endswith("." + noise) for noise in VIEW_NOISE_HOSTS)


VIEW_OVERLAY_JS = """
(text) => {
  let el = document.getElementById('vk-feed-view');
  if (!el) {
    el = document.createElement('div');
    el.id = 'vk-feed-view';
    el.style.cssText = 'position:fixed;top:8px;left:8px;z-index:2147483647;'
      + 'background:rgba(0,0,0,.82);color:#fff;font:600 14px/1.45 monospace;'
      + 'padding:8px 12px;border-radius:8px;pointer-events:none;max-width:92vw;'
      + 'white-space:pre-line';
    (document.body || document.documentElement).appendChild(el);
  }
  el.textContent = text;
}
"""


VIEW_SCROLL_JS = """
(share) => {
  // Высота того, что человек видит: на мобильном экране это визуальное окно
  // (visualViewport), innerHeight — запасной вариант для широкой выдачи.
  const vv = window.visualViewport || null;
  const vh = Math.round((vv && vv.height) || window.innerHeight || 0);
  const step = Math.max(300, Math.round(vh * share));
  window.scrollBy({top: step, left: 0, behavior: 'smooth'});
  const y = Math.round(window.scrollY || window.pageYOffset || 0);
  const h = Math.max(document.documentElement.scrollHeight,
                     document.body ? document.body.scrollHeight : 0);
  // Постов в ленте: широкая выдача помечает их data-post-id, мобильная (см.
  // view_mobile_context()) — своей разметкой, поэтому берём первое, что нашлось.
  let posts = 0;
  for (const sel of ['[data-post-id]', '.wall_item', 'div[id^="post"]']) {
    posts = document.querySelectorAll(sel).length;
    if (posts) break;
  }
  return {
    step: step,
    y: y,
    h: h,
    end: y + step + vh >= h - 40,
    posts: posts,
  };
}
"""


# Своя консоль VK: загрузчик статики ищет тег по префиксу
# window.vk.static.domain + путь, а страница пришла с другого CDN-зеркала
# (st7-14.vk.ru против st.vk.ru), и пишет «Not found blocking <адрес> asset from …».
# Файл при этом жив (200) и лента работает, а от User-Agent строка не зависит:
# живой Chromium с Android, iPhone и настольным UA даёт ту же пару строк (разница
# только в зеркале, которое выбрал сервер). В окне на виду это не ошибка окна и не
# отказ VK, поэтому глушим ровно эту строку и ничего больше — остальные
# предупреждения и ошибки VK остаются в консоли.
# Скрипт — самовызывающаяся функция: add_init_script вычисляет текст как выражение,
# и одна стрелочная функция без вызова только создалась бы (проверено: шум оставался).
VIEW_CONSOLE_NOISE = ("Not found blocking ",)
VIEW_CONSOLE_MUTE_JS = """
(() => {
  const marks = %s;
  const warn = console.warn.bind(console);
  console.warn = (...args) => {
    const first = args.length ? String(args[0]) : '';
    if (marks.some((mark) => first.includes(mark))) return;
    warn(...args);
  };
})();
""" % json.dumps(list(VIEW_CONSOLE_NOISE))


class FeedViewer:
    """Окно браузера на виду: показывает, как прогон обходит ленту.

    Обход постов идёт HTTP-запросами (newsfeed.search) — в браузере их не видно.
    Поэтому окно открывает ту же страницу поиска по тегу, что и сам обход, и
    подписывает её: какое окно времени берётся, какая это страница, сколько постов
    и от чьего аккаунта пришло. Чем искать тег — #тег или словом без решётки —
    окно берёт из того же tag_query, что и запросы прогона (см. feed_search_url),
    иначе оно показывало бы чужую выдачу.

    Окно живёт в своём потоке и в своём браузере: профиль не занимает (заходит по
    cookies сессии, которые ставит на домен .vk.ru/.vk.com — см. view_cookie_jar),
    `_browser_lock` не берёт. Поэтому обход его не ждёт: показывается последнее окно,
    а прогон работает с той же скоростью, что и со скрытым эмулятором.

    Рекламу и чужую статистику окно не грузит (см. VIEW_NOISE_HOSTS): ленте они не
    нужны, а в консоли окна оставляют 403/504/CORS и отнимают полосу у постов,
    которые VK отдаёт не сразу.

    Шум загрузчика статики VK в консоли («Not found blocking … asset from …») окно
    глушит (см. VIEW_CONSOLE_MUTE_JS): это предупреждение самого VK о теге на другом
    CDN-зеркале, файлы при этом живы, а лента и обход работают как обычно.

    Выдачу окно НЕ перезагружает — F5 оно не жмёт: страница отдаёт посты не сразу, и
    перезагрузка на каждом окне времени снова оставляла бы окно пустым. Окна времени
    сменяются прокруткой — посты в ленте догружаются сами, как при листании; страницу
    окно открывает заново, только если листать больше нечего (сменился тег или
    страница уехала/закрылась).

    Окно открывается мобильным экраном (390×844, тач, телефонный user-agent — см.
    view_mobile_context()): VK отдаёт ту же ленту лёгкой мобильной выдачей, и видно её
    так же, как её видит человек с телефона. Листает окно неровно, шагами каждые
    VIEW_SCROLL_MIN_SEC–VIEW_SCROLL_MAX_SEC секунды (см. _scroll_pause) — не только
    когда прогон взял новое окно времени, но и пока он его обходит (см. _tick).

    Нужна графическая сессия (show_browser / --show-browser); без показа окно не
    создаётся вовсе.
    """

    def __init__(
        self,
        session_path: Path | str,
        proxy_raw: str = "",
        label: str = "",
        *,
        timeout_ms: int = VIEW_LOAD_TIMEOUT_MS,
        tag_query: str = "hash",
    ) -> None:
        self.session_path = Path(session_path) if session_path else Path("")
        self.proxy = normalize_proxy(proxy_raw)
        self.label = label or ""
        self.timeout_ms = int(timeout_ms)
        # Чем искать тег в окне: «hash» — #тег, «word» — словом без решётки. Это та же
        # настройка tag_query, по которой идут запросы прогона (run_tags.resolve_tag_query),
        # поэтому окно показывает ровно ту выдачу, которую собирают.
        self.tag_query = normalize_tag_query(tag_query)
        self.shown = 0  # сколько раз окно успело показать окно обхода
        self.scrolls = 0  # сколько раз окно прокрутило выдачу (страницу оно не перезагружает)
        self.logins = 0  # сколько раз VK вместо ленты показал вход
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._pending: Optional[tuple[str, str, str]] = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cookies_from = ""
        self._cookies_at = 0.0
        self._warned_cookies = False
        self._warned_login = False
        self._failures = 0
        self._broken = False
        self._tag = ""  # какая выдача сейчас в окне ("" — не показана)
        self._caption = ""  # подпись этой выдачи: её и обновляет прокрутка (см. _tick)
        self._posts = 0  # постов в окне: это число окно пишет в свою подпись

    # --- что зовёт прогон ---
    def start(self) -> "FeedViewer":
        """Поднимает поток окна. Браузер откроется на первой же странице обхода."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="feed-view", daemon=True
            )
            self._thread.start()
        return self

    def visit(
        self, tag: str, caption: str = "", session_path: Optional[Path | str] = None
    ) -> None:
        """Показать в окне очередное окно обхода. Не ждёт и не бросает исключений.

        Обход никогда не должен вставать из-за окна, поэтому просьбы не копятся, а
        перезаписываются: успеет показать последнюю — уже хорошо.
        """
        if self._broken or self._stopped.is_set():
            return
        with self._lock:
            self._pending = (
                tag or "",
                caption or "",
                str(session_path or self.session_path),
            )
        self._wake.set()

    def stop(self, timeout: float = VIEW_STOP_SEC) -> None:
        """Закрывает окно: конец прогона, «Стоп» или Ctrl+C.

        Закрывает браузер поток окна — playwright не любит, когда его трогают из
        чужого потока. Если поток застрял на загрузке страницы, окно закроет он сам,
        как только страница дочитается (или процесс при выходе).
        """
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log("окно на виду: закрыть сразу не вышло — закроется вместе с прогоном")
                return
        self._close()

    def alive(self) -> bool:
        """True — окно ещё показывает (не сломалось и не остановлено)."""
        return not self._broken and not self._stopped.is_set()

    # --- поток окна: всё, что трогает браузер, живёт здесь ---
    def _loop(self) -> None:
        """Ждёт, что показать, и показывает. Ошибки окна обход не срывают."""
        try:
            with self._session() as pw:
                while not self._stopped.is_set():
                    # Ждём просьбу показать не дольше паузы между прокрутками: пока
                    # новых окон обхода нет, окно листает ленту (см. _tick), а просьба
                    # прогона будит его сразу (см. _take).
                    command = self._take(self._scroll_pause())
                    if self._stopped.is_set():
                        continue
                    if command is None:
                        self._tick()
                        continue
                    tag, caption, session_path = command
                    try:
                        if self._page is None:
                            self._open(pw)
                        self._apply_cookies(session_path)
                        self._show(tag, caption)
                    except Exception as exc:  # noqa: BLE001 — окно не срывает обход
                        self._note_failure(exc)
        except Exception as exc:  # noqa: BLE001 — нет playwright, браузера или экрана
            self._note_failure(exc)
        finally:
            self._close()

    def _take(self, timeout: float = VIEW_SCROLL_MAX_SEC) -> Optional[tuple[str, str, str]]:
        """Ждёт просьбу показать. Последняя важнее пропущенных.

        timeout — сколько ждать, если показывать нечего: окно выдерживает паузу до
        следующей прокрутки (см. _scroll_pause). Просьба прогона себя ждать не
        заставляет: _wake её поднимает, и ожидание кончается сразу.
        """
        if not self._wake.wait(timeout):
            return None
        self._wake.clear()
        with self._lock:
            command, self._pending = self._pending, None
        return command

    def _scroll_pause(self) -> float:
        """Пауза до следующей прокрутки: 0.2–0.45 с, каждый раз разная.

        Ровный шаг по таймеру — примета робота, а человек листает неровно: то
        быстрее, то медленнее. Верхняя граница заодно держит окно чутким к прогону:
        просьбу показать новое окно обхода оно замечает не позже самой длинной паузы.
        """
        return random.uniform(VIEW_SCROLL_MIN_SEC, VIEW_SCROLL_MAX_SEC)

    def _tick(self) -> None:
        """Листает ленту, пока прогон не попросил показать новое окно обхода.

        Обход молчит между окнами времени, и без этого окно стояло бы на месте: лента
        листается шагами каждые 0.2–0.45 с (см. _scroll_pause) — посты догружаются
        сами, как при листании пальцем, а в подписи видно, как растёт их число.
        Прокрутить не вышло (страница уехала на вход или закрылась) — выдачу больше не
        показываем: заново её откроет _render на следующем окне обхода, как это делает
        _show. Листать нечего, если выдачу ещё не показывали (тег пуст) или браузера
        нет.
        """
        if self._page is None or not self._tag:
            return
        if self._scroll_step(self._caption):
            return
        self._tag = ""  # листать нечего: следующее окно обхода откроет выдачу заново

    @contextlib.contextmanager
    def _session(self) -> Iterator[Any]:
        """Playwright в своём потоке (в тестах подменяется заглушкой)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - окружение без playwright
            raise VkAuthError("нужен playwright: pip install playwright") from exc
        with sync_playwright() as pw:
            yield pw

    def _open(self, pw) -> None:
        """Открывает окно браузера по cookies сессии — профиль не занимаем.

        Профиль занят обновлением сессии (F5), да и второй браузер на том же
        профиле Chromium не пустит. У окна на виду своя сессия: оно заходит по
        cookies, которые прогон и так использует для запросов.
        """
        if not has_display():
            raise VkAuthError(
                "показывать окно браузера не могу: графической сессии нет "
                f"({os_label()}) — выключите show_browser/--show-browser"
            )
        launch: dict[str, Any] = {"headless": False, "args": browser_args()}
        pw_proxy = proxy_to_playwright(self.proxy)
        if pw_proxy:
            launch["proxy"] = pw_proxy
        last_error: Exception | None = None
        for attempt in browser_launch_attempts():
            name = attempt.get("channel") or attempt.get("executable_path") or "chromium"
            try:
                browser = pw.chromium.launch(**attempt, **launch)
            except Exception as exc:  # noqa: BLE001 — не тот браузер, пробуем следующий
                last_error = exc
                if browser_not_installed(exc):
                    log(f"окно на виду: браузер {name} не установлен — пробую следующий")
                else:
                    log(f"окно на виду: браузер {name} не запустился: "
                        f"{str(exc).splitlines()[0][:160]}")
                continue
            self._browser = browser
            # Мобильный экран: VK отдаёт ленту телефонной выдачей — одна колонка
            # постов, которая листается пальцем (см. view_mobile_context()).
            self._context = browser.new_context(
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                **view_mobile_context(),
            )
            self._context.set_default_timeout(self.timeout_ms)
            try:
                # Предупреждения загрузчика статики VK («Not found blocking …») —
                # шум самого VK, а не ошибка окна: см. VIEW_CONSOLE_MUTE_JS. В окне
                # на виду они только пугают, поэтому глушим эту строку; ошибки
                # страницы и VK остаются в консоли как есть.
                self._context.add_init_script(VIEW_CONSOLE_MUTE_JS)
            except Exception as exc:  # noqa: BLE001 — без глушения окно всё равно покажет
                log(f"окно на виду: шум VK в консоли не заглушил ({exc})")
            try:
                # Матчер-функция: перехватываем только «шумные» адреса, а обычные
                # запросы окно делает само, как браузер (перехват всех их замедлил бы).
                self._context.route(view_noise_url, self._route_noise)
            except Exception as exc:  # noqa: BLE001 — без запрета окно всё равно покажет
                log(f"окно на виду: рекламу и статистику в окне не заглушил ({exc})")
            self._page = self._context.pages[0] if self._context.pages \
                else self._context.new_page()
            return
        raise VkAuthError(browser_missing_hint(last_error, "показывать обход ленты"))

    def _route_noise(self, route) -> None:
        """Не пускает в окно чужую рекламу/статистику (см. view_noise_url).

        Ленте эти запросы не нужны, а в консоли окна они оставляют 403/504/CORS и
        отнимают полосу у постов, которые VK отдаёт не сразу. Всё остальное
        пропускаем: без него окно показывало бы не ту выдачу, что собирает обход.
        """
        try:
            if view_noise_url(route.request.url):
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001 — один запрос не важнее целого окна
            pass

    def _apply_cookies(self, session_path: str) -> None:
        """Свежие cookies сессии в окно: после обновления сессии оно не разлогинится.

        Ставим их доменом (см. view_cookie_jar), а не по url: иначе VK видит только
        сам vk.ru, а его поддомены остаются анонимными, и лента не открывается.
        """
        if self._context is None:
            return
        now = time.time()
        if (session_path == self._cookies_from
                and now - self._cookies_at < VIEW_COOKIE_RELOAD_SEC):
            return
        data = load_session(Path(session_path)) if session_path else None
        cookies = dict((data or {}).get("cookies") or {})
        self._cookies_from, self._cookies_at = session_path, now
        if not has_login_cookie(cookies) and not self._warned_cookies:
            self._warned_cookies = True
            log(
                "окно на виду: "
                + ("в сессии нет cookies" if not cookies
                   else "в cookies сессии нет remixsid")
                + " — VK может показать вход"
            )
        if not cookies:
            return
        jar = view_cookie_jar(cookies)
        try:
            self._context.clear_cookies()
            self._context.add_cookies(jar)
        except Exception as exc:  # noqa: BLE001 — страницу всё равно покажем
            log(f"окно на виду: cookies сессии не подставил ({exc})")

    def _show(self, tag: str, caption: str) -> None:
        """Показывает очередное окно обхода: только прокрутка ленты, без F5.

        Перезагружать выдачу нельзя: страница приходит за секунду, а посты в ней —
        только через 5–10 секунд, и новая перезагрузка снова оставляет окно пустым
        (посты не успевают прогрузиться). Поэтому страница живёт, окно времени
        сменяется прокруткой — посты догружаются сами, как при листании, — а выдачу
        окно открывает заново только там, где листать больше нечего: сменился тег
        (в окне другая выдача) или прокрутить не вышло (страница уехала на вход или
        закрылась). VK показал вход — листать нечего, ждём свежие cookies.
        """
        self._caption = caption  # подпись этой выдачи: её обновляет и прокрутка (_tick)
        if self._tag != tag:
            if self._render(tag, caption):
                self.shown += 1
            return
        if self._scroll_step(caption):
            self.shown += 1
            return
        if self._render(tag, caption):  # страница уехала или закрылась
            self.shown += 1

    def _scroll_step(self, caption: str) -> bool:
        """Прокручивает выдачу вниз — этим окно и показывает смену окна времени.

        True — прокрутили и обновили подпись: окно времени новое, страница та же,
        посты в ленте догружаются сами. False — прокрутить не вышло (страница
        закрылась или уехала): пусть решает _render. Зовут её и окна обхода (_show),
        и паузы между ними (_tick) — пауза между прокрутками в _scroll_pause.
        """
        if self._page is None:
            return False
        try:
            pos = self._page.evaluate(VIEW_SCROLL_JS, VIEW_SCROLL_SHARE) or {}
        except Exception as exc:  # noqa: BLE001 — подпись и лента важнее
            log(f"окно на виду: прокрутить выдачу не вышло ({exc})")
            return False
        self._posts = int(pos.get("posts") or 0)
        self.scrolls += 1
        note = VIEW_POSTS_NOTE.format(n=self._posts)
        # Посты в подписи видно, как они прибавляются: это и есть проверка, что лента живая.
        self._overlay(f"{caption}{note}" if caption else note.strip())
        return True

    def _render(self, tag: str, caption: str) -> bool:
        """Открывает страницу поиска по тегу (vk.ru → vk.com) и подписывает окно.

        Так выдачу окно открывает заново — только когда листать больше нечего:
        сменился тег или страница уехала (см. _show). True — окно показало саму
        выдачу обхода (подпись и счётчик постов — с чистого листа). False — VK
        показал вход: тогда показ не считается, а cookies сессии окно перечитает на
        следующем окне обхода (VK обновляет их, когда эмулятор жмёт F5). Пустая
        страница (прокси молчит) — это сбой: пробуем другой хост, как и ошибку
        перехода. Тег в адресе — тот же, что ушёл в запрос прогона: с решёткой или
        словом без неё (см. self.tag_query и feed_search_url).
        """
        self._tag = ""  # выдачу пока не показали: до конца _render это не факт
        errors: list[str] = []
        for host in FEED_HOSTS:
            url = feed_search_url(tag, host, self.tag_query)
            try:
                self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
            except Exception as exc:  # noqa: BLE001 — vk.ru не открылся, пробуем vk.com
                errors.append(f"{url}: {str(exc).splitlines()[0][:120]}")
                continue
            if _page_is_blank(self._page):
                errors.append(f"{url}: окно осталось пустым (нет сети/прокси)")
                continue
            shown_url = self._page_url()
            if is_login_page_url(shown_url):
                self._note_login(shown_url)
                self._overlay(
                    f"{caption}{VIEW_LOGIN_NOTE}" if caption else VIEW_LOGIN_NOTE.strip()
                )
                return False
            self._tag = tag
            self._posts = 0  # свежая страница: постов в ней пока не считали
            self._overlay(caption)
            return True
        raise VkAuthError(" | ".join(errors) or "страница поиска не открылась")

    def _page_url(self) -> str:
        """Адрес, на котором окно оказалось (после редиректов VK)."""
        try:
            return str(self._page.url or "")
        except Exception:  # noqa: BLE001 — окно закрыли извне
            return ""

    def _note_login(self, url: str) -> None:
        """VK вместо ленты показал вход — говорим один раз и просим свежие cookies.

        Так окно выглядит, когда cookies сессии устарели: сессия в файле обновилась
        (F5 в эмуляторе), а окну они ещё не переставлены. Обход от этого не страдает
        — он ходит запросами и на страницу не смотрит, — но человек должен понимать,
        что в окне не лента.
        """
        self.logins += 1
        self._cookies_at = 0.0  # на следующем окне обхода — перечитать cookies сессии
        if self._warned_login:
            return
        self._warned_login = True
        log(
            f"окно на виду: VK просит вход вместо ленты ({url[:90]}) — "
            f"cookies аккаунта {self.label or '—'} окну не подошли, "
            "перечитаю их на следующем окне обхода (обход идёт запросами, входа не требует)"
        )

    def _overlay(self, caption: str) -> None:
        """Подпись поверх страницы: какое окно обхода показано прямо сейчас."""
        if not caption:
            return
        try:
            self._page.evaluate(VIEW_OVERLAY_JS, caption)
        except Exception as exc:  # noqa: BLE001 — подпись украшение, страница важнее
            log(f"окно на виду: подпись не поставил ({exc})")

    def _note_failure(self, exc: Exception) -> None:
        """Окно сломалось: говорим в лог и больше не мешаем прогону."""
        self._failures += 1
        if self._failures == 1:
            log(f"окно на виду: показать обход не вышло ({exc}) — "
                "прогон продолжается как обычно")
        elif self._failures <= VIEW_MAX_FAIL_LOG:
            log(f"окно на виду: та же беда ({exc})")
        if self._failures >= VIEW_MAX_FAIL_LOG:
            self._broken = True
            log("окно на виду: больше не пробую — обход идёт скрыто, как раньше")

    def _close(self) -> None:
        """Закрывает окно (и браузер) — сколько бы раз это ни звали."""
        self._tag = ""  # окна нет: выдачу в нём мы больше не показываем
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 — окно уже могло закрыться само
                pass
        self._context = self._browser = self._page = None


def refresh_from_cookies(
    session_path: Path = SESSION_FILE,
    proxy_raw: str = "",
    app_id: int = WEB_APP_ID,
) -> dict:
    """Обновляет токен по сохранённым cookies — браузер не нужен.

    Самый дешёвый путь, но VK часто режет его ошибкой «too much tries»: тогда фон
    переходит на профиль браузера с F5 (token_via_feed_reload). Профиль браузера в
    сессии при этом сохраняется — иначе этот запасной путь пропал бы навсегда.
    """
    data = load_session(session_path)
    if not data:
        raise VkAuthError(f"нет файла сессии: {session_path}")
    cookies = data.get("cookies") or {}
    if not has_login_cookie(cookies):
        raise VkAuthError("в сессии нет remixsid — нужен повторный вход в браузере")
    proxy = normalize_proxy(proxy_raw or data.get("proxy") or "")
    token_data = request_web_token_http(cookies, proxy=proxy, app_id=app_id)
    payload = _save_refreshed_session(
        Path(session_path), data, token_data, cookies, proxy, source="cookies"
    )
    log(f"токен обновлён: {session_path}")
    return payload


def refresh_token(
    session_path: Path = SESSION_FILE, proxy_raw: str = "", headless: bool = True
) -> dict:
    """Свежий токен: сначала профиль браузера с F5, затем обмен cookies.

    Единая точка для CLI: тем же порядком обновляет сессию фон прогона (сначала
    дешёвый обмен cookies, при отказе — профиль+F5). Здесь наоборот — браузер первым,
    потому что --refresh запускает человек и хочет результат, а не отказ VK.

    headless=False — окно браузера на виду: эмулятор открывается видимым, сам
    заходит в ленту и жмёт F5 (CLI --refresh показывает окно, если не передан
    --headless).
    """
    try:
        return refresh_via_browser(session_path, proxy_raw, headless=headless)
    except Exception as exc:  # noqa: BLE001 — нет профиля, браузер не поднялся, капча
        log(f"профиль браузера (F5) не помог ({exc}) — пробую обмен cookies…")
    return refresh_from_cookies(session_path, proxy_raw)


# CLI
def _pick_account(args) -> Optional[Account]:
    if args.login_value:
        return Account(
            login=args.login_value,
            password=args.password or "",
            twofa_secret=args.twofa or "",
            proxy_raw=args.proxy or "",
        )
    accounts = load_accounts()
    if not accounts:
        log(f"нет аккаунтов в {ACCOUNTS_FILE} — вход вручную (телефон вводите сами)")
        return None
    idx = max(1, int(args.account or 1)) - 1
    if idx >= len(accounts):
        log(f"аккаунт #{idx + 1} не найден, беру первый")
        idx = 0
    return accounts[idx]


def _print_status(session_path: Path = SESSION_FILE) -> int:
    data = load_session(session_path)
    if not data:
        print(f"сессии нет: {session_path}")
        return 1
    user = data.get("user") or {}
    expires = float(data.get("token_expires_at") or 0)
    left = int(expires - time.time()) if expires else 0
    cookies = data.get("cookies") or {}
    print(f"файл:      {session_path}")
    print(f"логин:     {data.get('login') or '—'}")
    print(
        "профиль:   "
        f"{user.get('first_name', '')} {user.get('last_name', '')} "
        f"(id {user.get('id') or data.get('user_id') or '—'})"
    )
    print(f"токен:     {session_token(data)[:12]}…")
    print(f"истекает:  {left // 60} мин ({left} с)" if left > 0 else "истекает:  истёк")
    print(
        f"cookies:   {len(cookies)} шт, "
        f"remixsid={'да' if has_login_cookie(cookies) else 'нет'}"
    )
    print(f"прокси:    {proxy_host_port(data.get('proxy') or '') or '—'}")
    print(f"обновлено: {data.get('updated_at') or '—'}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Авторизация VK для vk_feed_parser")
    parser.add_argument("--login", action="store_true", help="вход в браузере (по умолчанию)")
    parser.add_argument("--qr", action="store_true", help="сразу предложить вход по QR-коду")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="обновить токен: профиль браузера (лента через F5), затем cookies",
    )
    parser.add_argument("--check", action="store_true", help="проверить сессию и поиск по тегу")
    parser.add_argument("--status", action="store_true", help="показать сохранённую сессию")
    parser.add_argument("--headless", action="store_true", help="браузер без окна")
    parser.add_argument("--no-autofill", action="store_true", help="не подставлять логин/пароль")
    parser.add_argument("--account", type=int, default=1, help="номер аккаунта из accounts.txt")
    parser.add_argument("--login-value", default="", help="телефон/логин (перебивает accounts.txt)")
    parser.add_argument("--password", default="", help="пароль")
    parser.add_argument("--twofa", default="", help="base32 секрет 2FA")
    parser.add_argument("--proxy", default="", help="прокси: host:port:user:pass")
    parser.add_argument("--session", default=str(SESSION_FILE), help="файл сессии")
    parser.add_argument("--timeout", type=int, default=450, help="сколько ждать вход, сек")
    parser.add_argument("--url", default="", help="стартовый URL входа")
    parser.add_argument(
        "--password-only",
        action="store_true",
        help="только номер+пароль: без запросов SMS/MAX и автоподтверждений",
    )
    parser.add_argument(
        "--profile",
        default=str(PROFILE_DIR),
        help="папка профиля Chromium (для второго аккаунта — своя)",
    )
    args = parser.parse_args(argv)

    session_path = Path(args.session)
    if args.status:
        return _print_status(session_path)

    if args.refresh:
        try:
            # Окно на виду: --refresh запускает человек, ему и смотреть, как эмулятор
            # обновляет ленту. Скрыть — флаг --headless.
            payload = refresh_token(session_path, args.proxy, headless=not args.headless)
        except Exception as exc:  # noqa: BLE001
            log(f"ошибка обновления: {exc}")
            return 1
        log(f"профиль: {payload.get('user') or payload.get('user_id')}")
        return 0

    if args.check:
        data = load_session(session_path) or {}
        if not token_alive(data):
            try:
                refresh_token(session_path, args.proxy)
            except Exception as exc:  # noqa: BLE001
                log(f"токен не обновлён ({exc}) — проверяю сохранённый")
        from vk_client import VkFeedClient  # локальный импорт: не плодим циклы

        client = VkFeedClient.from_session(session_path)
        tag = "маникюр"
        tags_file = ACCOUNTS_FILE.parent / "tags.txt"
        if tags_file.exists():
            for line in tags_file.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("//"):
                    tag = s.lstrip("#")
                    break
        result = client.search_hashtag(tag, count=3)
        print(
            f"поиск #{tag}: {len(result['items'])} постов, "
            f"next_from={'есть' if result.get('next_from') else 'нет'}"
        )
        for post in result["items"][:3]:
            print(f"  {post['date']}  {post['url']}  {post['text'][:70]!r}")
        return 0

    account = _pick_account(args)
    proxy = args.proxy or (resolve_proxy(account) if account else "")
    if not proxy:
        # Раньше подставлялся первый прокси из proxies.txt — чужой IP.
        # Теперь вход идёт с IP этой машины, и это видно в логе.
        log(
            f"предупреждение: у аккаунта {(account.login if account else '—')} "
            "нет своего прокси — вход пойдёт напрямую"
        )
    try:
        payload = login_interactive(
            account,
            proxy,
            timeout=args.timeout,
            start_url=args.url,
            use_qr=args.qr,
            autofill=not args.no_autofill,
            headless=args.headless,
            password_only=args.password_only,
            profile_dir=Path(args.profile),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"ошибка входа: {exc}")
        return 1
    user = payload.get("user") or {}
    log(
        f"готово: {user.get('first_name', '')} {user.get('last_name', '')} "
        f"(id {payload.get('user_id')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
