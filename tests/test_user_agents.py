#!/usr/bin/env python3
"""Мобильный User-Agent: только Android/iPhone и новый на каждый запрос страницы.

    python tests/test_user_agents.py

Сеть не нужна: VK подменяется локальным HTTP-сервером, который записывает заголовки
каждого запроса. Проверяем то, ради чего пул и заведён:

  1) пул user_agents: в нём только телефоны (Android и iPhone), настольных строк нет,
     а is_mobile() отличает телефон от ПК;
  2) random_user_agent() никогда не повторяет прошлый UA — телефон меняется на каждом
     запросе;
  3) заголовки и импресонация берутся из того же устройства: у Android — chrome_android
     и client hints с версией Chrome из UA и «?1», у iPhone — safari_ios и без подсказок
     (Safari их не шлёт);
  4) живой клиент прогона: стартовый UA сессии сразу мобильный, а каждый следующий
     запрос страницы уходит другим телефоном — это видно по заголовкам на сервере;
  5) HTTP-сессия vk_auth (обмен cookies на токен, проверка прокси) тоже стартует с
     мобильного UA и умеет менять его на каждый запрос;
  6) окна ленты (F5 и окно обхода) открываются мобильным экраном, и каждое новое окно
     берёт другой телефонный UA; настольный UA остался только у окна входа --login —
     это форма для человека.
"""
from __future__ import annotations

import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import user_agents  # noqa: E402
import vk_auth  # noqa: E402
import vk_client  # noqa: E402
from vk_client import VkFeedClient  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


DESKTOP_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DESKTOP_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)

ANDROID_UA = next(ua for ua in user_agents.MOBILE_USER_AGENTS if "Android" in ua)
IPHONE_UA = next(ua for ua in user_agents.MOBILE_USER_AGENTS if "iPhone" in ua)

# # #
# 1. Пул: только телефоны
# # #
check(
    len(user_agents.MOBILE_USER_AGENTS) >= 8
    and all(user_agents.is_mobile(ua) for ua in user_agents.MOBILE_USER_AGENTS),
    f"весь пул — мобильные UA: {len(user_agents.MOBILE_USER_AGENTS)} строк",
)
check(
    any("Android" in ua for ua in user_agents.MOBILE_USER_AGENTS)
    and any("iPhone" in ua for ua in user_agents.MOBILE_USER_AGENTS),
    "в пуле есть и Android, и iPhone",
)
check(
    not any(
        mark in ua.lower()
        for ua in user_agents.MOBILE_USER_AGENTS
        for mark in user_agents.DESKTOP_MARKS
    ),
    "настольных строк (Windows NT/Macintosh/X11/CrOS) в пуле нет",
)
check(
    not user_agents.is_mobile(DESKTOP_WINDOWS)
    and not user_agents.is_mobile(DESKTOP_MAC)
    and not user_agents.is_mobile(""),
    "is_mobile() не считает телефоном Windows, Mac и пустую строку",
)

# # #
# 2. Смена UA: прошлый не повторяется
# # #
random.seed(7)
prev = ""
seen: list[str] = []
repeated = 0
for _ in range(30):
    ua = user_agents.random_user_agent(prev)
    if ua == prev:
        repeated += 1
    seen.append(ua)
    prev = ua
check(repeated == 0, f"random_user_agent() не повторяет прошлый UA ({repeated} повторов)")
check(
    len(set(seen)) > 1 and all(user_agents.is_mobile(ua) for ua in seen),
    f"30 бросков дают разные телефоны и все мобильные: {len(set(seen))} вариантов",
)

# # #
# 3. UA ↔ импресонация ↔ client hints
# # #
android_headers = user_agents.request_headers(ANDROID_UA)
iphone_headers = user_agents.request_headers(IPHONE_UA)
check(
    user_agents.impersonate_for(ANDROID_UA) == "chrome_android"
    and android_headers.get("User-Agent") == ANDROID_UA
    and android_headers.get("sec-ch-ua-mobile") == "?1"
    and android_headers.get("sec-ch-ua-platform") == '"Android"'
    and 'Chrome";v="131"' in (android_headers.get("sec-ch-ua") or ""),
    f"Android: chrome_android + подсказки того же Chrome: {android_headers}",
)
check(
    user_agents.impersonate_for(IPHONE_UA) == "safari_ios"
    and iphone_headers.get("User-Agent") == IPHONE_UA
    and iphone_headers.get("sec-ch-ua") is None
    and iphone_headers.get("sec-ch-ua-mobile") is None
    and iphone_headers.get("sec-ch-ua-platform") is None,
    f"iPhone: safari_ios и без client hints (Safari их не шлёт): {iphone_headers}",
)
try:
    user_agents.impersonate_for(DESKTOP_WINDOWS)
    desktop_refused = False
except ValueError:
    desktop_refused = True
check(desktop_refused, "настольный UA импресонацию телефона не получает")

# # #
# 4. Живой клиент прогона: новый телефон на каждый запрос страницы
# # #
SEEN: list[dict] = []


class _Recorder(BaseHTTPRequestHandler):
    """Сервер-заглушка VK: запоминает заголовки каждого запроса страницы."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        body = b'{"response": {"items": [], "count": 0}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()

old_bases, old_gap = vk_client.API_BASES, vk_client.call_gap()
vk_client.API_BASES = [base]
vk_client.set_call_gap(0, 0)
random.seed(5)
try:
    client = VkFeedClient("vk1.a.test", "", pause=0)
    initial = client.user_agent
    session_ua = client._http().headers.get("User-Agent")
    for _ in range(12):
        client.call("newsfeed.search", {"q": "#тест"})
finally:
    vk_client.API_BASES = old_bases
    vk_client.set_call_gap(*old_gap)
    server.shutdown()

check(
    user_agents.is_mobile(initial) and session_ua == initial,
    f"стартовый UA сессии — сразу телефон (не ПК): {session_ua!r}",
)
check(len(SEEN) == 12, f"до сервера дошло 12 запросов страниц: {len(SEEN)}")
uas = [h.get("user-agent") or "" for h in SEEN]
check(
    all(user_agents.is_mobile(ua) for ua in uas),
    "каждый запрос страницы — телефонный UA",
)
check(
    all(uas[i] != uas[i - 1] for i in range(1, len(uas)))
    and len(set(uas)) > 1
    and client.user_agent == uas[-1],
    f"UA меняется на каждом запросе (соседние не повторяются; "
    f"{len(set(uas))} разных из {len(uas)}) и клиент помнит последний",
)
android_seen = [h for h in SEEN if "Android" in (h.get("user-agent") or "")]
iphone_seen = [h for h in SEEN if "iPhone" in (h.get("user-agent") or "")]
check(
    bool(android_seen) and bool(iphone_seen),
    f"за 12 запросов встретились и Android, и iPhone: "
    f"{len(android_seen)}/{len(iphone_seen)}",
)
check(
    all(
        h.get("sec-ch-ua-mobile") == "?1"
        and h.get("sec-ch-ua-platform") == '"Android"'
        and "signed-exchange" in (h.get("accept") or "")
        for h in android_seen
    ),
    "Android-запросы: client hints «?1»/Android и Accept мобильного Chrome "
    "(импресонация тот же телефон, а не настольный)",
)
check(
    all(
        not any(key.startswith("sec-ch-ua") for key in h)
        and "signed-exchange" not in (h.get("accept") or "")
        for h in iphone_seen
    ),
    "iPhone-запросы: client hints нет и Accept как у Safari",
)

# # #
# 5. HTTP-сессия vk_auth: стартовый мобильный UA + смена на каждый запрос
# # #
session = vk_auth._http_session()
start_ua = session.headers.get("User-Agent") or ""
forced_ua = vk_auth._http_session(user_agent=DESKTOP_WINDOWS).headers.get("User-Agent") or ""
check(
    user_agents.is_mobile(start_ua)
    and user_agents.is_mobile(forced_ua)
    and forced_ua != DESKTOP_WINDOWS,
    f"сессия vk_auth стартует телефоном, настольный UA не подставляется: {start_ua!r}",
)
random.seed(9)
rotated = [vk_auth.rotate_user_agent(session) for _ in range(3)]
check(
    len(set(rotated)) == 3
    and start_ua not in rotated
    and all(user_agents.is_mobile(ua) for ua in rotated)
    and session.headers.get("User-Agent") == rotated[-1],
    f"rotate_user_agent() меняет телефон на каждый запрос: "
    f"{len(set(rotated))} разных из 3",
)
last_ua = rotated[-1]
hints_ok = (
    session.headers.get("sec-ch-ua-mobile") == "?1"
    if "Android" in last_ua
    else session.headers.get("sec-ch-ua") is None
)
check(
    hints_ok,
    "подсказки сессии совпадают с последним телефоном (после iPhone их нет)",
)

# # #
# 6. Окна ленты и исходники: телефон на каждое окно, ПК — только у входа
# # #
ctx1 = vk_auth.view_mobile_context()
ctx2 = vk_auth.view_mobile_context()
check(
    ctx1["viewport"] == {"width": 390, "height": 844}
    and ctx1["is_mobile"] is True
    and ctx1["has_touch"] is True
    and ctx1["device_scale_factor"] == 3.0,
    f"окно ленты — телефонный экран: {ctx1['viewport']}",
)
check(
    user_agents.is_mobile(ctx1["user_agent"]) and ctx1["user_agent"] != ctx2["user_agent"],
    "каждое новое окно ленты берёт другой телефонный UA",
)
check(
    "Android" in ctx1["user_agent"]
    and "iPhone" not in ctx1["user_agent"]
    and all("iPhone" not in vk_auth.view_mobile_context()["user_agent"] for _ in range(20)),
    "окно ленты не выдаёт себя за iPhone: UA совпадает с движком Chromium",
)
browser_ua = user_agents.random_browser_user_agent()
browser_next = user_agents.random_browser_user_agent(browser_ua)
check(
    "Android" in browser_ua and "Android" in browser_next and browser_ua != browser_next,
    "random_browser_user_agent() меняет телефон и остаётся Android-строкой",
)

vk_src = (ROOT / "src" / "vk_auth.py").read_text(encoding="utf-8")
client_src = (ROOT / "src" / "vk_client.py").read_text(encoding="utf-8")
pool = " ".join(user_agents.MOBILE_USER_AGENTS)
check(
    vk_src.count("**view_mobile_context()") == 2 and "VIEW_MOBILE_CONTEXT" not in vk_src,
    "мобильный экран ставят и окно обхода, и заход в ленту через F5",
)
check(
    vk_src.count('"user_agent": BROWSER_UA') == 1 and "Windows NT" in vk_src,
    "настольный UA остался только у окна входа --login (форма для человека)",
)
check(
    'impersonate="chrome"' not in client_src
    and 'impersonate="chrome"' not in vk_src
    and "Windows NT" not in pool
    and "Macintosh" not in pool,
    "настольной импресонации и настольных строк в пуле нет",
)

print()
if FAILED:
    print(f"ПРОВАЛЕНО: {len(FAILED)}")
    for msg in FAILED:
        print(" -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
