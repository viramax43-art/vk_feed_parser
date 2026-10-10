#!/usr/bin/env python3
"""Авторизация VK и получение user token из сессии браузера.

VK ID не даёт войти одним HTTP-запросом: нужен реальный браузер, и почти всегда на
шаге подтверждения появляется капча, код или QR. Поэтому вход интерактивный: окно
Chromium открывается, скрипт сам подставляет телефон и пароль, а капчу (код из
приложения или QR) завершает человек в открытом окне. Как только на домене vk.ru
появляется remixsid, скрипт забирает cookies, выменивает их на user token
(login.vk.ru/?act=web_token, app_id=7879029 — тот же токен, что использует сайт VK)
и сохраняет data/session.json.

Запуск:
    python src/vk_auth.py --login      # вход в браузере (капчу решает человек)
    python src/vk_auth.py --qr         # то же, но сразу предлагает QR-код
    python src/vk_auth.py --refresh    # обновить токен из сохранённых cookies
    python src/vk_auth.py --check      # проверить сессию и тестовый поиск
    python src/vk_auth.py --status     # что лежит в data/session.json

Если у аккаунта нет доступа к коду (SMS, MAX), помогает --password-only: скрипт
подставляет номер и пароль, а все подтверждения (капчу, «Пройти проверку») делает
человек, без автозапроса кодов. VK пускает по паролю 1–2 аккаунта с одного IP,
поэтому второму аккаунту нужен свежий прокси и отдельный профиль:
python src/vk_auth.py --account 2 --password-only --profile data/vk_browser_profile_2

Профиль браузера по умолчанию — data/vk_browser_profile; у аккаунта со своим
профилем — data/vk_browser_profile_<номер>, повторный вход нужен редко.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import (  # noqa: E402
    Account,
    load_accounts,
    normalize_proxy,
    proxies_from_file,
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
LOGIN_START_URLS = ["https://vk.ru/login", "https://vk.com/login"]

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


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
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
    return f"+{digits}" if raw.strip().startswith("+") or len(digits) == 11 else digits


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


def _http_session(proxy: str = ""):
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:  # pragma: no cover
        raise VkAuthError("нужен пакет curl_cffi: pip install curl_cffi") from exc
    session = cffi_requests.Session(impersonate="chrome")
    session.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )
    proxy = normalize_proxy(proxy)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


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
        try:
            resp = session.post(
                url,
                data={"version": "1", "app_id": str(app_id)},
                headers={**headers, "Origin": origin, "Referer": origin + "/"},
                timeout=30,
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
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
        try:
            resp = session.post(f"{base}/{method}", data=body, timeout=timeout)
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
        body = page.locator("body").inner_text(timeout=2500)
    except Exception:  # noqa: BLE001
        return ""
    body = re.sub(r"\s+", " ", body or "").strip()
    return body[:limit]


def detect_stage(page) -> str:
    """Что сейчас просит VK: капчу, код, QR, пароль."""
    if _any_visible(page, CAPTCHA_HINTS):
        return "captcha"
    if _any_visible(page, OTP_HINTS):
        return "otp"
    if _any_visible(page, PASSWORD_SELECTORS):
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
            text = (loc.inner_text(timeout=1000) or "").strip()
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
            if loc.count() and loc.is_visible(timeout=1000):
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
            if loc.count() and loc.is_visible(timeout=1000):
                loc.click(timeout=4000)
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
            if loc.count() and loc.is_visible(timeout=1000):
                loc.click()
                log(f"нажал «{pattern}» — перехожу к вводу пароля")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


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
        value = field.input_value(timeout=1000) or ""
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
            if not loc.count() or not loc.is_visible(timeout=500):
                continue
            text = re.sub(r"\s+", " ", (loc.inner_text(timeout=1500) or "")).strip()
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
            if loc.count() and loc.is_visible(timeout=600):
                loc.click(timeout=3000)
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
        field.click(timeout=3000)
    except Exception:  # noqa: BLE001
        try:
            field.focus(timeout=3000)
        except Exception:  # noqa: BLE001
            return False
    # Очистка: fill("") работает на любой ОС; на macOS Control+A не выделяет
    # текст (нужен Cmd), из-за чего старые цифры смешивались с новыми.
    for _ in range(3):
        try:
            field.fill("", timeout=3000)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(local, delay=60)
        except Exception:  # noqa: BLE001
            pass
        typed = _field_digits(field)
        # маска VK может показывать «7» в поле — сверяем по хвосту номера
        if typed.endswith(local) and len(typed) <= len(local) + 1:
            return True
        time.sleep(0.5)
    try:
        field.fill(local, timeout=4000)
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
        return not (loc.input_value(timeout=800) or "").strip()
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
        time.sleep(0.5)
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
            ">>> VK просит код (MAX/SMS). Введите его в окне или подождите 25 с — "
            "скрипт сам переключится на ввод пароля"
        )
    if stage == "qr":
        return (
            ">>> QR: можно отсканировать код приложением VK («Сканировать QR»), "
            "иначе через 25 с скрипт сам перейдёт к вводу пароля"
        )
    return ""


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
    launch: dict[str, Any] = {
        "headless": headless,
        "locale": "ru-RU",
        "viewport": {"width": 1366, "height": 900},
        "user_agent": BROWSER_UA,
        "timezone_id": "Europe/Moscow",
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    pw_proxy = proxy_to_playwright(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy

    os.environ["VK_AUTH_PROXY"] = proxy
    log(f"профиль браузера: {profile_dir}")
    log(f"прокси: {proxy or '— (напрямую)'}")
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
        context = pw.chromium.launch_persistent_context(str(profile_dir), **launch)
        try:
            context.set_default_timeout(15000)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                start_url or LOGIN_START_URLS[0],
                wait_until="domcontentloaded",
                timeout=120000,
            )

            phone_attempts = 0
            password_attempts = 0
            other_clicks = 0
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
            deadline = time.time() + max(60, timeout)

            while time.time() < deadline:
                if page.is_closed():
                    raise VkAuthError("окно браузера закрыто — вход прерван")
                cookies = cookie_map(context.cookies())
                if has_login_cookie(cookies):
                    log("вход выполнен: получен remixsid")
                    break

                in_restore = "/restore" in (page.url or "")
                if in_restore and restore_hits < RESTORE_MAX_RETURNS:
                    restore_hits += 1
                    log(
                        "VK увёл в «Восстановление доступа» — возвращаюсь на страницу "
                        f"входа (попытка {restore_hits}/{RESTORE_MAX_RETURNS}), "
                        "в restore ничего не нажимаю"
                    )
                    try:
                        page.goto(
                            start_url or LOGIN_START_URLS[0],
                            wait_until="domcontentloaded",
                            timeout=90000,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log(f"не удалось вернуться на страницу входа: {exc}")
                    last_stage = ""
                    stage_since = time.time()
                    time.sleep(2)
                    continue

                popup = _read_popup(page)
                if popup:
                    low = popup.lower()
                    if any(hint in low for hint in FLOOD_HINTS):
                        cooldown_until = max(cooldown_until, time.time() + 120)
                        log(f"VK: {popup} → пауза 120 с (повтор? смените прокси)")
                    else:
                        log(f"VK показал окно: {popup}")
                    _dismiss_popup(page)
                    last_beat = time.time()
                    _screenshot(page, "vk_login_popup.png")
                    time.sleep(2)
                    continue

                if time.time() < cooldown_until:
                    time.sleep(5)
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
                    and time.time() - last_phone_fill > 12
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
                        elif phone_attempts and time.time() - last_submit > 10:
                            last_submit = time.time()
                            pressed = _submit(page)
                            log(f"номер уже в поле, повторяю отправку: {pressed}")

                if stage == "otp":
                    if password_only and not sms_requested:
                        sms_requested = True
                        log(
                            "VK просит код, режим --password-only: запросов SMS/MAX "
                            "не делаю — подтвердите вход в окне (скрипт только ждёт)"
                        )
                    if (
                        autofill
                        and not sms_requested
                        and not password_only
                        and not in_restore
                        and time.time() - otp_since > 25
                        and _otp_input_empty(page)
                    ):
                        log("код не введён — открываю способы, запрашиваю SMS")
                        if _click_text(
                            page,
                            (
                                "Подтвердить другим способом",
                                "Другие способы",
                                "Другой способ",
                            ),
                        ):
                            sms_requested = True
                            time.sleep(3)
                            if _click_text(page, ("Код на телефон", "Получить SMS")):
                                log(">>> код отправлен по SMS — введите его в окне")
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
                            field.click(timeout=3000)
                            field.fill(account.password or "", timeout=4000)
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
                time.sleep(3)

            cookies = cookie_map(context.cookies())
            if not has_login_cookie(cookies):
                _screenshot(page, "vk_login_timeout.png")
                raise VkAuthError(
                    f"вход не завершён за {timeout} с (последний экран: {last_stage}). "
                    f"Скриншот: {LOG_DIR / 'vk_login_timeout.png'}"
                )

            try:
                page.goto("https://vk.ru/feed", wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
            except Exception:  # noqa: BLE001
                pass
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
                timeout=30000,
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
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


def refresh_via_browser(session_path: Path, proxy_raw: str = "", timeout: int = 90) -> dict:
    """Новый токен через сохранённый профиль браузера — без участия человека.

    Профиль, в котором уже выполнен вход, отдаёт remixsid сразу, браузер
    скрытый. Если VK требует капчу/код, за timeout вход не завершится: будет
    VkAuthError, тогда нужен ручной вход --login.
    """
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
        headless=True,
        profile_dir=Path(profile_dir),
    )


def refresh_from_cookies(
    session_path: Path = SESSION_FILE,
    proxy_raw: str = "",
    app_id: int = WEB_APP_ID,
) -> dict:
    """Обновляет токен по сохранённым cookies — браузер не нужен."""
    data = load_session(session_path)
    if not data:
        raise VkAuthError(f"нет файла сессии: {session_path}")
    cookies = data.get("cookies") or {}
    if not has_login_cookie(cookies):
        raise VkAuthError("в сессии нет remixsid — нужен повторный вход в браузере")
    proxy = normalize_proxy(proxy_raw or data.get("proxy") or "")
    token_data = request_web_token_http(cookies, proxy=proxy, app_id=app_id)
    profile: dict = {}
    try:
        profile = validate_token(token_data["access_token"], proxy)
    except Exception as exc:  # noqa: BLE001
        log(f"предупреждение: токен не подтвердился ({exc})")
    payload = build_session_payload(
        token_data,
        cookies,
        login=data.get("login", ""),
        proxy=proxy,
        profile=profile,
        source="cookies",
    )
    save_session(payload, session_path)
    log(f"токен обновлён: {session_path}")
    return payload


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
    print(f"прокси:    {data.get('proxy') or '—'}")
    print(f"обновлено: {data.get('updated_at') or '—'}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Авторизация VK для vk_feed_parser")
    parser.add_argument("--login", action="store_true", help="вход в браузере (по умолчанию)")
    parser.add_argument("--qr", action="store_true", help="сразу предложить вход по QR-коду")
    parser.add_argument("--refresh", action="store_true", help="обновить токен из cookies")
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
    parser.add_argument("--timeout", type=int, default=900, help="сколько ждать вход, сек")
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
            payload = refresh_from_cookies(session_path, args.proxy)
        except Exception as exc:  # noqa: BLE001
            log(f"ошибка обновления: {exc}")
            return 1
        log(f"профиль: {payload.get('user') or payload.get('user_id')}")
        return 0

    if args.check:
        data = load_session(session_path) or {}
        if not token_alive(data):
            try:
                refresh_from_cookies(session_path, args.proxy)
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
        pool = proxies_from_file()
        if pool:
            proxy = normalize_proxy(pool[0])
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
