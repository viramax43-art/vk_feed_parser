#!/usr/bin/env python3
"""Клиент VK API для поиска постов по тегам (newsfeed.search).

search_hashtag(tag, count, start_from, start_time, end_time) отдаёт одну страницу,
iter_pages(...) листает дальше по next_from с паузами. Есть повторные проходы для
вылавливания новых постов, прокси, ретраи на flood control (код 6/9) и понятная
ошибка на капчу (14).

Чем искать тег — хештегом (#тег) или словом без решётки — решает настройка tag_query
(страница «Теги» в панели, флаг --tag-query, vk_client.set_tag_query): у VK это две
разные выдачи, и режим один на прогон (см. search_query).

Токен берётся из data/session.json (см. vk_auth.py); вручную —
VkFeedClient(token="...").
"""
from __future__ import annotations

import json
import re
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import normalize_proxy  # noqa: E402
from paths import SESSION_FILE  # noqa: E402
from settings import normalize_tag_query  # noqa: E402
from user_agents import impersonate_for, random_user_agent, request_headers  # noqa: E402
from vk_auth import (  # noqa: E402
    API_BASES,
    API_VERSION,
    VkApiError,
    VkAuthError,
    load_session,
    token_alive,
)

SEARCH_METHOD = "newsfeed.search"
FIELDS = "screen_name,first_name,last_name,name,photo_100"

# Ограничения newsfeed.search (dev.vk.com/ru/method/newsfeed.search):
#   * count — не больше 200 записей за один вызов (больше VK отвергает кодом 100);
#   * глубже первых 1000 результатов выдача не идёт (даже с offset/start_from).
# Второе ограничение обрабатывает probe.run_window: окно, упёршееся в 1000,
# помечается need_split и делится по времени — остаток собирают половинки,
# а не теряется на повторяющихся страницах.
MAX_COUNT = 200
MAX_RESULTS = 1000

RETRY_CODES = {6, 9, 10}  # too many requests / flood control / internal
CAPTCHA_CODES = {14}
TOKEN_CODES = {5, 15, 27, 28, 1117}  # 1117 — access token has expired

# Лимит скорости: не чаще одного запроса за MIN_CALL_GAP_SEC на аккаунт.
# VK считает всплески запросов признаком автоматизации, поэтому интервал держим
# общим для всех клиентов одного логина (aккаунт мог сменить поток после отлёжки).
# Интервал между запросами одного аккаунта: каждый раз случайно 3.5–4.5 с,
# фиксированный шаг VK видит как автоматику.
MIN_CALL_GAP_SEC = 3.5
MAX_CALL_GAP_SEC = 4.5
_throttle_lock = threading.Lock()
_last_call_at: dict[str, float] = {}


def set_call_gap(min_gap: float, max_gap: float) -> tuple[float, float]:
    """Меняет интервал между запросами одного аккаунта (settings.json).

    Возвращает то, что получилось: так вызывающий может показать лимит в логе.
    Кто не трогал настройку — работает на прежних 7–9 с.
    """
    global MIN_CALL_GAP_SEC, MAX_CALL_GAP_SEC
    lo = max(0.0, float(min_gap))
    hi = max(lo, float(max_gap))
    MIN_CALL_GAP_SEC, MAX_CALL_GAP_SEC = lo, hi
    return lo, hi


def call_gap() -> tuple[float, float]:
    """Текущий лимит скорости на аккаунт, секунды."""
    return MIN_CALL_GAP_SEC, MAX_CALL_GAP_SEC


# Чем искать тег в запросе (newsfeed.search): "hash" — #тег, как было всегда, "word" —
# слово без решётки: посты, где тег встречается в тексте. У VK это РАЗНЫЕ выдачи,
# поэтому режим один на весь прогон — настройка tag_query (страница «Теги») или флаг
# --tag-query — и живёт здесь, а не в клиенте: клиентов много, а запрос у них должен
# получиться один и тот же. Окно обхода показывает ту же выдачу (vk_auth.feed_search_url).
TAG_QUERY = "hash"


def set_tag_query(mode: object) -> str:
    """Меняет, чем искать тег: "hash" — #тег, "word" — слово без решётки.

    Возвращает то, что получилось: вызывающий показывает режим в логе. Значение
    нормализуется как в настройках (см. settings.normalize_tag_query): непонятное —
    это прежнее поведение, хештег.
    """
    global TAG_QUERY
    TAG_QUERY = normalize_tag_query(mode)
    return TAG_QUERY


def tag_query() -> str:
    """Текущий режим тега в запросе: "hash" или "word"."""
    return TAG_QUERY


def search_query(query: str, mode: Optional[str] = None) -> str:
    """Строка q для newsfeed.search: "#тег" или "тег" без решётки.

    «hash» (по умолчанию, как было всегда): решётку дописываем, если её нет, — VK
    отдаёт посты с этим хештегом. «word»: решётку снимаем — VK ищет слово в тексте
    постов. Пустой тег ведёт себя как раньше: решётка одна, а в «word» — пустой q.
    """
    raw = str(query or "").strip()
    if (TAG_QUERY if mode is None else str(mode)) == "word":
        return extract_tag(raw)
    return raw if raw.startswith("#") else f"#{raw}"


# Авто-режим тега (settings tag_query = "auto"): за какое окно и сколькими постами
# пробуем оба запроса, прежде чем выбрать. Два запроса на тег один раз за всё
# время (выбор запоминается), поэтому окно суток и count 50 — этого хватает, чтобы
# увидеть разницу «7 постов хештегом против 171 словом». 50, а не 20: у VK выдача
# обрезается лимитом, и на маленьком count оба запроса упирались бы в потолок и
# давали «равенство» там, где словом постов заметно больше.
MODE_PROBE_HOURS = 24
MODE_PROBE_COUNT = 50


def detect_tag_mode(
    client: "VkFeedClient",
    tag: str,
    *,
    hours: float = MODE_PROBE_HOURS,
    count: int = MODE_PROBE_COUNT,
    now: Optional[float] = None,
) -> tuple[str, int, int]:
    """Чем искать тег — хештегом или словом: сравнивает две выдачи за пробное окно.

    VK отдаёт по «#тег» и по «тег» РАЗНЫЕ выдачи. У нишевых тегов хештег почти
    пустой (люди пишут слово без решётки), и прогон по хештегу выглядит как «0
    постов», хотя по слову лента не кончается. Здесь два дешёвых запроса за
    последние `hours` часов: возвращаем режим, где постов больше, и сами числа —
    их пишут в лог, чтобы было видно, почему выбран именно он.
    """
    end = int(now if now is not None else time.time()) + 1
    start = end - int(max(1.0, float(hours)) * 3600)
    clean = extract_tag(tag)
    hash_items = len(
        client.search_hashtag(
            clean, count=count, start_time=start, end_time=end, mode="hash"
        ).get("items")
        or []
    )
    word_items = len(
        client.search_hashtag(
            clean, count=count, start_time=start, end_time=end, mode="word"
        ).get("items")
        or []
    )
    return ("word" if word_items > hash_items else "hash"), hash_items, word_items


def _throttle(key: str, gap_min: Optional[float] = None, gap_max: Optional[float] = None) -> None:
    """Ждёт, пока с прошлого запроса этого аккаунта пройдёт случайный интервал."""
    if not key:
        return
    gap_min = MIN_CALL_GAP_SEC if gap_min is None else gap_min
    gap_max = MAX_CALL_GAP_SEC if gap_max is None else gap_max
    while True:
        with _throttle_lock:
            now = time.time()
            gap = random.uniform(gap_min, gap_max)
            wait = gap - (now - _last_call_at.get(key, 0.0))
            if wait <= 0:
                _last_call_at[key] = now
                return
        time.sleep(wait)


class VkCaptchaError(VkApiError):
    """VK требует капчу: нужен captcha_sid + решение человеком/сервисом."""


class VkTokenError(VkApiError):
    """Токен недействителен — нужен повторный вход (vk_auth.py --login)."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_tag(query: str) -> str:
    """Из запроса «#маникюр» или «маникюр» делает «маникюр»."""
    return re.sub(r"^#+", "", (query or "").strip())


def _author_maps(response: dict) -> tuple[dict, dict]:
    profiles = {
        int(p.get("id") or 0): p for p in (response.get("profiles") or []) if isinstance(p, dict)
    }
    groups = {
        -abs(int(g.get("id") or 0)): g for g in (response.get("groups") or []) if isinstance(g, dict)
    }
    return profiles, groups


def _author_of(owner_id: int, profiles: dict, groups: dict) -> dict:
    if owner_id < 0:
        group = groups.get(owner_id) or {}
        return {
            "kind": "group",
            "id": owner_id,
            "name": group.get("name") or f"club{-owner_id}",
            "screen_name": group.get("screen_name") or "",
            "url": "https://vk.com/" + (group.get("screen_name") or f"club{-owner_id}"),
        }
    profile = profiles.get(owner_id) or {}
    name = " ".join(
        x for x in (profile.get("first_name"), profile.get("last_name")) if x
    ).strip()
    return {
        "kind": "user",
        "id": owner_id,
        "name": name or f"id{owner_id}",
        "screen_name": profile.get("screen_name") or "",
        "url": "https://vk.com/" + (profile.get("screen_name") or f"id{owner_id}"),
    }


def attachment_stats(item: dict) -> dict:
    """Краткая сводка вложений: типы и счётчики (фото/видео/ссылки)."""
    types: list[str] = []
    photos = videos = audios = docs = 0
    for att in item.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "")
        if kind:
            types.append(kind)
        if kind == "photo":
            photos += 1
        elif kind == "video":
            videos += 1
        elif kind == "audio":
            audios += 1
        elif kind in ("doc", "doc_graffiti"):
            docs += 1
        elif kind == "link" and att.get("link", {}).get("url"):
            types.append("link")
    return {
        "types": types,
        "photos": photos,
        "videos": videos,
        "audios": audios,
        "docs": docs,
    }


def normalize_post(
    item: dict,
    tag: str,
    profiles: Optional[dict] = None,
    groups: Optional[dict] = None,
) -> dict:
    """Приводит элемент ответа newsfeed.search к плоской записи для базы и Excel."""
    profiles = profiles or {}
    groups = groups or {}
    owner_id = int(item.get("owner_id") or item.get("source_id") or 0)
    post_id = int(item.get("post_id") or item.get("id") or 0)
    date = int(item.get("date") or 0)
    stats = attachment_stats(item)
    likes = item.get("likes") or {}
    comments = item.get("comments") or {}
    reposts = item.get("reposts") or {}
    views = item.get("views") or {}
    return {
        "tag": tag,
        "key": f"{owner_id}_{post_id}",
        "type": item.get("type") or "post",
        "owner_id": owner_id,
        "post_id": post_id,
        "url": f"https://vk.com/wall{owner_id}_{post_id}",
        "date": date,
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(date)) if date else "",
        "text": (item.get("text") or "").strip(),
        "author": _author_of(owner_id, profiles, groups),
        "likes": int(likes.get("count") or 0),
        "comments": int(comments.get("count") or 0),
        "reposts_count": int(reposts.get("count") or 0),
        "views": int(views.get("count") or 0),
        "attachments": stats["types"],
        "photos": stats["photos"],
        "videos": stats["videos"],
        "audios": stats["audios"],
        "docs": stats["docs"],
        "attachments_raw": item.get("attachments") or [],
    }


class VkFeedClient:
    """Обёртка над VK API: один клиент = один токен (+свой прокси)."""

    def __init__(
        self,
        token: str,
        proxy: str = "",
        *,
        api_version: str = API_VERSION,
        timeout: int = 15,
        pause: float = 0.2,
        cookies: Optional[dict] = None,
        login: str = "",
    ) -> None:
        self.token = (token or "").strip()
        if not self.token:
            raise VkAuthError(
                "пустой access_token — выполните вход: python src/vk_auth.py --login"
            )
        self.proxy = normalize_proxy(proxy)
        self.api_version = api_version
        self.timeout = timeout
        self.pause = max(0.0, float(pause))
        self.cookies = cookies or {}
        self.login = login or ""
        # Стартовый UA — сразу телефонный (Android/iPhone), без настольного Chrome:
        # VK отдаёт телефону другую выдачу, а первый же запрос не должен выдавать ПК.
        # Дальше UA меняется на каждый запрос страницы — см. next_user_agent().
        self.user_agent = random_user_agent()
        self._session = None

    @classmethod
    def from_session(
        cls,
        session_path: Path = SESSION_FILE,
        proxy: Optional[str] = None,
        *,
        pause: float = 0.2,
        auto_refresh: bool = True,
    ) -> "VkFeedClient":
        """Восстанавливает клиент по сохранённой сессии.

        Прокси задаётся явно: None — взять сохранённый в сессии, "" — напрямую,
        строка — этот прокси. Пустая строка не путается с «не задано», иначе
        аккаунту с direct подставился бы старый прокси из файла сессии.
        """
        data = load_session(session_path) or {}
        use_proxy = (data.get("proxy") or "") if proxy is None else proxy
        if auto_refresh and not token_alive(data):
            try:
                from vk_auth import refresh_from_cookies

                log(f"токен истёк — обновляю из cookies ({Path(session_path).name})")
                data = refresh_from_cookies(session_path, use_proxy)
            except Exception as exc:  # noqa: BLE001
                log(f"обновить токен не удалось: {exc}")
        token = str(data.get("access_token") or "")
        if not token:
            raise VkAuthError(
                f"в {session_path} нет access_token — выполните: "
                "python src/vk_auth.py --login"
            )
        return cls(
            token,
            use_proxy,
            api_version=data.get("api_version") or API_VERSION,
            pause=pause,
            cookies=data.get("cookies") or {},
            login=data.get("login") or "",
        )

    def set_proxy(self, proxy: str) -> str:
        """Ставит клиенту другой прокси: адрес аккаунта меняют на ходу.

        Прогон меняет аккаунту прокси, когда VK молча отдаёт пустую выдачу даже
        после F5. Сессия curl_cffi создаётся лениво и запоминает прокси при
        создании, поэтому правим и живую сессию — иначе запросы пошли бы по
        старому адресу. Возвращает нормализованный прокси, который теперь в работе.
        """
        self.proxy = normalize_proxy(proxy)
        if self._session is not None:
            self._session.proxies = (
                {"http": self.proxy, "https": self.proxy} if self.proxy else {}
            )
        return self.proxy

    def next_user_agent(self) -> str:
        """Новый мобильный UA на следующий запрос страницы (никогда не повторяет прошлый).

        VK считает повторы одного UA и одной связки «телефон + браузер» признаком
        автоматизации, поэтому каждая страница уходит с другим телефоном: Android или
        iPhone, см. user_agents.py. Возвращает UA, который теперь в работе.
        """
        self.user_agent = random_user_agent(self.user_agent)
        return self.user_agent

    def _http(self):
        if self._session is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as exc:  # pragma: no cover
                raise VkAuthError("нужен пакет curl_cffi: pip install curl_cffi") from exc
            # Импресонация — под тот же телефон, что и UA: TLS, HTTP/2 и client hints
            # мобильного Chrome/Safari, а не настольного.
            self._session = cffi_requests.Session(
                impersonate=impersonate_for(self.user_agent)
            )
            self._session.headers.update(
                {
                    **request_headers(self.user_agent),
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                }
            )
            if self.proxy:
                self._session.proxies = {"http": self.proxy, "https": self.proxy}
        return self._session

    def _raise_for_error(self, err: dict, method: str) -> None:
        code = int(err.get("error_code") or 0)
        msg = str(err.get("error_msg") or "unknown")
        if code in TOKEN_CODES:
            raise VkTokenError(code, f"{msg} ({method})", err)
        if code in CAPTCHA_CODES:
            raise VkCaptchaError(code, f"{msg} ({method})", err)
        raise VkApiError(code, f"{msg} ({method})", err)

    def call(
        self,
        method: str,
        params: dict,
        *,
        retries: int = 3,
        pause_override: Optional[float] = None,
    ) -> Any:
        """POST на api.vk.*/method/<method> с ретраями на flood control.

        Каждый запрос страницы уходит новым телефоном (см. next_user_agent): UA,
        импресонация и client hints берутся из одного устройства — Android или iPhone.
        """
        body = {k: v for k, v in params.items() if v is not None and v != ""}
        body["access_token"] = self.token
        body["v"] = self.api_version
        # Интервал держим по аккаунту: логин, а если его нет — по началу токена
        _throttle(self.login or self.token[:12])
        last: Exception = VkAuthError("нет попыток")
        session = self._http()
        for attempt in range(1, max(1, retries) + 1):
            for base in API_BASES:
                user_agent = self.next_user_agent()
                try:
                    resp = session.post(
                        f"{base}/{method}",
                        data=body,
                        timeout=self.timeout,
                        headers=request_headers(user_agent),
                        impersonate=impersonate_for(user_agent),
                    )
                    payload = resp.json()
                except Exception as exc:  # noqa: BLE001
                    last = VkAuthError(f"{base}: {exc}")
                    continue
                if not isinstance(payload, dict):
                    last = VkAuthError(f"{base}: ответ не JSON")
                    continue
                if "error" not in payload:
                    if pause_override is None:
                        if self.pause:
                            time.sleep(self.pause)
                    elif pause_override:
                        time.sleep(pause_override)
                    return payload.get("response", payload)
                err = payload["error"]
                code = int(err.get("error_code") or 0)
                if code in RETRY_CODES and attempt < max(1, retries):
                    backoff = 1.5 * attempt
                    log(
                        f"VK {code} ({err.get('error_msg')}) — пауза {backoff} с "
                        f"и повтор ({attempt}/{retries})"
                    )
                    time.sleep(backoff)
                    last = VkApiError(code, str(err.get("error_msg")), err)
                    break  # повторяем запрос
                self._raise_for_error(err, method)
        raise last


    # #
    # Поиск по тегам
    # #
    def search(
        self,
        query: str,
        *,
        count: int = 50,
        start_from: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        extended: int = 1,
        mode: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Одна страница newsfeed.search: сырой ответ VK.

        Чем искать тег — хештегом (#тег) или словом без решётки — решает настройка
        tag_query (vk_client.set_tag_query) или явный `mode` (авто-режим тега:
        у VK это разные выдачи, см. search_query).

        `count` обрезается до лимита метода (1..200): настройку правят руками, а
        `--count` в консоли принимает любое число — VK на count > 200 отвечает
        ошибкой 100, и окно падало бы целиком вместо того, чтобы добрать страницы.
        """
        count = max(1, min(MAX_COUNT, int(count)))
        params: dict = {
            "q": search_query(query, mode),
            "count": count,
            "extended": extended,
            "fields": FIELDS,
        }
        if start_from:
            params["start_from"] = start_from
        if start_time:
            params["start_time"] = int(start_time)
        if end_time:
            params["end_time"] = int(end_time)
        params.update({k: v for k, v in extra.items() if v is not None})
        response = self.call(SEARCH_METHOD, params)
        return response if isinstance(response, dict) else {}

    def search_hashtag(
        self,
        tag: str,
        *,
        count: int = 50,
        start_from: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> dict:
        """Страница по хештегу с уже нормализованными постами.

        `mode` — чем искать этот тег сейчас ("hash"/"word"); None — общий режим
        прогона (vk_client.set_tag_query). Авто-режим тега передаёт его сюда, чтобы
        один тег шёл словом, а другой — хештегом (см. run_tags.tag_mode_for).
        """
        clean = extract_tag(tag)
        raw = self.search(
            clean,
            count=count,
            start_from=start_from,
            start_time=start_time,
            end_time=end_time,
            mode=mode,
        )
        profiles, groups = _author_maps(raw)
        items = raw.get("items") or []
        posts = [
            normalize_post(item, clean, profiles, groups)
            for item in items
            if isinstance(item, dict)
        ]
        return {
            "tag": clean,
            "items": posts,
            "next_from": raw.get("next_from") or "",
            # Документированное поле ответа — total_count; `count` оставляем
            # запасным вариантом (разные версии API отвечают по-разному), а
            # совсем без него считаем по факту отданного.
            "total": int(raw.get("total_count") or raw.get("count") or len(posts)),
            "profiles": len(profiles),
            "groups": len(groups),
            "raw_count": len(items),
        }

    def iter_pages(
        self,
        tag: str,
        *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        start_from: str = "",
        count: int = 50,
        max_pages: int = 10,
        sleep_between: float = 0.4,
        mode: Optional[str] = None,
    ) -> Iterator[dict]:
        """Прокрутка по next_from: одна нормализованная страница за раз."""
        cursor = start_from or ""
        for page in range(1, max(1, max_pages) + 1):
            result = self.search_hashtag(
                tag,
                count=count,
                start_from=cursor or None,
                start_time=start_time,
                end_time=end_time,
                mode=mode,
            )
            result["page"] = page
            result["start_from"] = cursor
            yield result
            cursor = result.get("next_from") or ""
            if not cursor:
                break
            if sleep_between:
                time.sleep(sleep_between)


    def run_passes(
        self,
        tag: str,
        *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        count: int = 50,
        max_pages: int = 10,
        passes: int = 1,
        pause_between_passes: int = 30,
        stop_when_no_new_pages: int = 2,
        on_page: Optional[Callable[[dict], int]] = None,
        mode: Optional[str] = None,
    ) -> dict:
        """Повторные проходы по тегу: passes раз заново с первой страницы.

        on_page(page) возвращает число новых постов (0 — страница дублей).
        Проход прерывается после stop_when_no_new_pages «пустых» страниц подряд:
        так ловим свежие посты и не жжём лимит API.
        """
        stats = {"pages": 0, "seen": 0, "new": 0, "passes": 0}
        for pass_no in range(1, max(1, passes) + 1):
            stats["passes"] = pass_no
            empty_streak = 0
            for page in self.iter_pages(
                tag,
                start_time=start_time,
                end_time=end_time,
                count=count,
                max_pages=max_pages,
                sleep_between=self.pause,
                mode=mode,
            ):
                stats["pages"] += 1
                stats["seen"] += len(page["items"])
                fresh = on_page(page) if on_page else len(page["items"])
                stats["new"] += max(0, fresh)
                empty_streak = empty_streak + 1 if fresh <= 0 else 0
                if empty_streak >= max(1, stop_when_no_new_pages):
                    break
            if pass_no < max(1, passes) and pause_between_passes:
                log(f"#{tag}: проход {pass_no} завершён, пауза {pause_between_passes} с")
                time.sleep(pause_between_passes)
        return stats


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ручная проверка newsfeed.search")
    parser.add_argument("tag", nargs="?", default="маникюр")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--hours", type=int, default=0, help="посты за последние N часов")
    parser.add_argument("--session", default=str(SESSION_FILE))
    parser.add_argument(
        "--proxy",
        default=None,
        help="прокси для запросов; direct — без прокси; по умолчанию — из сессии",
    )
    args = parser.parse_args(argv)

    start = int(time.time()) - 3600 * args.hours if args.hours else None
    client = VkFeedClient.from_session(Path(args.session), args.proxy)
    for page in client.iter_pages(
        args.tag, start_time=start, count=args.count, max_pages=args.pages
    ):
        print(
            f"страница {page['page']}: постов {len(page['items'])}, "
            f"всего у VK {page['total']}, next_from={page['next_from'][:24]!r}"
        )
        for post in page["items"]:
            print(
                f"  {post['date_str']}  {post['author']['name']}  "
                f"{post['likes']}/{post['comments']}/{post['views']}  "
                f"{post['url']}  {post['text'][:60]!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
