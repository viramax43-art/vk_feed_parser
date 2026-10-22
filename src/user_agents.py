#!/usr/bin/env python3
"""Мобильные User-Agent для запросов к VK: только Android и iPhone, без настольных.

VK отдаёт одной и той же сессии разную выдачу настольному браузеру и телефону, а
повтор одного и того же UA на каждой странице — признак робота. Поэтому прогон
ходит в VK телефоном: UA берётся из этого пула и меняется на каждый запрос страницы
(см. vk_client.VkFeedClient.call), а стартовый UA сессии — тоже мобильный.

В пуле — живые связки «телефон + браузер» последних версий: Chrome на Android и
Safari на iPhone. Настольных строк здесь нет и быть не должно (их видно по
«Windows NT», «Macintosh», «X11», «CrOS»): на них VK отдаёт другую выдачу, а
пользователь просил мобильные. Проверка — is_mobile().

Кроме самого UA запрос несёт «client hints» (sec-ch-ua*): Chrome на Android их шлёт,
Safari на iPhone — нет. Если оставить подсказки от настольного Chrome при мобильном
UA, VK видит противоречие, поэтому request_headers() собирает заголовки под выбранный
телефон, а импресонацию curl_cffi (TLS, HTTP/2, порядок заголовков) подбирает
impersonate_for() — тоже под телефон (chrome_android / safari_ios).
"""
from __future__ import annotations

import random
import re
from typing import Optional

# Android: Chrome на живых телефонах (Pixel, Samsung, Xiaomi, realme, vivo).
ANDROID_USER_AGENTS = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; 2201117TG) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; RMX3630) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; V2309) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
)

# iPhone: Safari (в iOS все браузеры ходят с одним и тем же WebKit-движком).
IPHONE_USER_AGENTS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_8 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
)

MOBILE_USER_AGENTS = ANDROID_USER_AGENTS + IPHONE_USER_AGENTS

# Приметы настольного клиента: по ним VK отличает ПК от телефона.
DESKTOP_MARKS = ("windows nt", "macintosh", "x11", "cros", "linux x86_64", "linux i686")

# Версия Chrome в подсказках, если её не удалось взять из UA (в строках пула она есть).
DEFAULT_CHROME_MAJOR = "131"


def is_mobile(user_agent: str) -> bool:
    """Мобильный ли UA: телефон видно по Android/iPhone и «Mobile», ПК — по приметам."""
    ua = (user_agent or "").strip().lower()
    if not ua:
        return False
    if any(mark in ua for mark in DESKTOP_MARKS):
        return False
    return ("android" in ua or "iphone" in ua) and "mobile" in ua


def random_user_agent(previous: str = "") -> str:
    """Случайный мобильный UA, не тот же самый, что прошлый: телефон меняется каждый раз.

    Предыдущий исключается, чтобы «смена» не оказалась пустой: два запроса подряд с
    одним и тем же UA — ровно то, от чего этот пул и заведён.
    """
    prev = (previous or "").strip()
    pool = [ua for ua in MOBILE_USER_AGENTS if ua != prev] or list(MOBILE_USER_AGENTS)
    return random.choice(pool)


def random_browser_user_agent(previous: str = "") -> str:
    """UA для настоящего браузера (Playwright/Chromium): только Chrome на Android.

    Браузеру нельзя подсовывать строку iPhone: снаружи это Safari/WebKit, а изнутри
    страницы видно Chrome — navigator.userAgentData, window.chrome, движок JS. Такое
    расхождение и есть настоящая примета подмены (одного UA мало: VK сверяет его с
    тем, что рассказывает сам браузер), поэтому окно ленты и F5-обновление сессии
    ходят Android-строкой, которая движку соответствует.

    HTTP-клиенту iPhone-строки по-прежнему можно: там под них подбирается и TLS-
    отпечаток (см. impersonate_for → safari_ios), то есть противоречия не возникает.
    Предыдущий UA исключается — телефон меняется на каждое окно.
    """
    prev = (previous or "").strip()
    pool = [ua for ua in ANDROID_USER_AGENTS if ua != prev] or list(ANDROID_USER_AGENTS)
    return random.choice(pool)


def impersonate_for(user_agent: str) -> str:
    """Импресонация curl_cffi под телефон: Android — chrome_android, iPhone — safari_ios.

    TLS-отпечаток, HTTP/2 и порядок заголовков у мобильного Chrome и мобильного Safari
    разные, поэтому импресонация берётся из того же устройства, что и UA.
    """
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "chrome_android"
    if "iphone" in ua:
        return "safari_ios"
    raise ValueError(f"не телефонный User-Agent: {user_agent!r}")


def _chrome_major(user_agent: str) -> str:
    """Версия Chrome из UA: client hints должны говорить то же, что и строка UA."""
    found = re.search(r"Chrome/(\d+)", user_agent or "")
    return found.group(1) if found else DEFAULT_CHROME_MAJOR


def request_headers(user_agent: str) -> dict[str, Optional[str]]:
    """Заголовки запроса под выбранный телефон: UA + client hints того же вида.

    Android (Chrome) шлёт sec-ch-ua с той же версией, что и UA, и «?1» (мобильный) с
    платформой Android. Safari на iPhone клиентских подсказок не шлёт вовсе — здесь они
    удаляются (None значит «убрать заголовок»: его мог подставить curl_cffi), иначе
    запрос выглядел бы настольным Chrome с поддельным UA.
    """
    ua = (user_agent or "").strip()
    headers: dict[str, Optional[str]] = {"User-Agent": ua}
    if "android" in ua.lower():
        major = _chrome_major(ua)
        headers.update(
            {
                "sec-ch-ua": f'"Chromium";v="{major}", "Not_A Brand";v="24", '
                             f'"Google Chrome";v="{major}"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
            }
        )
    else:
        headers.update(
            {"sec-ch-ua": None, "sec-ch-ua-mobile": None, "sec-ch-ua-platform": None}
        )
    return headers
