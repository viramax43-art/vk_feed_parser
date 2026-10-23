#!/usr/bin/env python3
"""Прогон по списку хештегов: все аккаунты работают параллельно над окнами времени.

Запуск:
    python src/run_tags.py data/pilot.txt      # свой список тегов
    python src/run_tags.py --hours 6           # только последние 6 часов
    python src/run_tags.py --hours 6 --run-hours 6   # работать 6 часов: непрерывно собирать новые посты
    python src/run_tags.py --passes 5          # + 4 повторных прохода за новыми постами
    python src/run_tags.py --watch             # повторять проходы, пока не остановят
    python src/run_tags.py --no-refresh        # не перепроверять уже собранные часы
    python src/run_tags.py --no-resume         # не добивать прерванный прогон
    python src/run_tags.py --validate-first    # проверить аккаунты, потом запустить
    python src/run_tags.py --no-session-refresh # не менять токены во время прогона

Тег режется на окна по window_hours, окна идут в общую очередь, каждый аккаунт
берёт следующее свободное. Окна выдаются порциями (воркеров × windows_per_worker):
порция держит очередь полной, но прогон на ней не заканчивается — теги идут по
кругу и подкидывают следующие окна, пока период не пройден целиком. Поэтому
«за 30 суток» с десятками тегов собирается одним запуском, а не по порции на
каждый «Старт». Период — это ЧТО собирать, а не сколько работать: один проход
заканчивается, как только период охвачен (в логе «период 6.0 ч из 6.0 ч (100%)»),
поэтому «за 6 часов» на коротком периоде пролетает за минуту. Сколько работать —
--run-hours H (в панели «Работать, ч», она же заполняется по периоду): процесс
непрерывно собирает посты до H часов, считая от «Старт» — пока время не вышло,
он снова и снова проверяет свежий хвост периода и добирает только новые посты.
Как часто проверять — --repeat-pause (по умолчанию 30 с из настроек): круг, который
не принёс ни одного нового поста, ждёт эту паузу (в логе «новых постов нет — проверю
снова») — иначе тот же пустой хвост уходил бы в VK каждые пару секунд и плодил окна.
«Стоп» прерывает. Свежий хвост периода (refresh_tail_hours, по умолчанию 6 ч)
перепроверяется даже собранным: лента меняется — приходят запоздавшие посты
(ВК индексирует их с задержкой), обновляются лайки и комментарии;
--no-refresh — не перепроверять, --full — перепроверить весь период.
Перепроверка качается строго внутри периода: дальше [start, end) сбор не уходит,
даже если окно было записано шире. В отчёте «период» — это запрошенный отрезок
(не сумма длин окон), поэтому «период 6.0 ч из 6.0 ч (100%)» = период охвачен.
Если VK обрезал выдачу окна (нашёл больше, чем отдал), окно
делится пополам. Повторный проход берёт только [дата последнего найденного
поста − нахлёст, сейчас]. Прогресс окон и курсоры лежат в SQLite: после Ctrl+C
запуск продолжит с того же места. Недобитые окна прошлого запуска добираются,
но только те, где есть несобранные часы: если все часы окна уже собраны другими
окнами, оно снимается с очереди — лента отдаёт по нему то же самое (в логе
«постов N, новых 0»), а непройденные часы ждут.

Аккаунтов может быть больше, чем воркеров (--workers): они работают пулом с
ротацией. Аккаунт ушёл в отлёжку (капча, лимиты, пустые выдачи) или выбыл (нужен
повторный вход) — воркер берёт следующий живой; статусы смотреть через
python src/account_pool.py --status.

Аккаунты — data/accounts.txt, сессии — data/session.json и
data/session_<телефон>.json (python src/vk_auth.py --login).

Токен VK живёт ограниченное время (в сессии — token_expires_at, обычно около
суток), а прогон «за 30 суток» идёт часами: пока идёт работа, фоновый поток раз
в session_refresh_minutes меняет access_token аккаунтов на свежий (cookies →
профиль браузера) и подставляет его работающим клиентам, поэтому живые аккаунты
не выбывают посреди прогона и ручной вход не нужен. Выключить: --no-session-refresh
или "session_refresh_minutes": 0 в data/settings.json.

Эмулятор браузера (профиль ленты + F5) работает скрыто, а сам обход идёт
HTTP-запросами (newsfeed.search) — браузера в нём нет вовсе. Хочется видеть, что
происходит, — включите показ окна: --show-browser или "show_browser": true в
data/settings.json (в панели — галочка «показывать окно браузера»). Тогда показ
идёт по двум линиям:
  * окно обхода — открывает ту же выдачу по тегу, что берёт прогон (хештег или слово —
    как выбрано в tag_query), и подписывает её: какое окно времени обходится прямо
    сейчас, номер окна, аккаунт. Живёт весь прогон и само закрывается с его концом
    («Стоп», Ctrl+C);
  * окно эмулятора — мелькает, когда сессию надо освежить через ленту (F5) или
    обновить токен: заходит в ленту, жмёт F5, держится пару секунд и закрывается.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe as probe_mod  # noqa: E402
from account_pool import (  # noqa: E402
    DEAD,
    HOLD,
    READY,
    AccountPool,
    PoolAccount,
    load_pool_accounts,
    validate_accounts,
)
from accounts import Account, load_accounts, proxy_host_port  # noqa: E402
from export_xlsx import export_posts  # noqa: E402
from paths import ACCOUNTS_FILE, EXPORTS_DIR, SESSION_FILE, TAGS_FILE  # noqa: E402
from platform_compat import has_display, os_label  # noqa: E402
from proxy_stock import swap_account_proxy  # noqa: E402
from settings import TAG_QUERY_LABELS, load_settings, normalize_tag_query  # noqa: E402
from redact import redact  # noqa: E402
from state_db import DB  # noqa: E402
from vk_client import (  # noqa: E402
    MODE_PROBE_HOURS,
    VkFeedClient,
    detect_tag_mode,
    extract_tag,
    set_call_gap,
    set_tag_query,
)

_print_lock = threading.Lock()
_stop_flag = threading.Event()
_export_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {"jobs": 0, "split": 0, "new": 0, "pages": 0, "errors": 0, "captcha": 0, "token": 0,
          "throttle": 0, "dupes": 0, "junk": 0}
# «Тихий ноль»: VK принял запрос, но отдал пустую страницу, хотя посты по этим часам
# есть (проверяется канарейкой в empty_is_throttle). Это отказ на конкретный запрос,
# а не «постов нет»: раньше окно ждало 15/45/90/150/300 с и получало ту же пустоту —
# запрос ведь оставался тем же. Поэтому меняем сам запрос: свежая сессия (F5), другой
# адрес (свободный прокси из запаса), другой count, окно пополам, — а когда шаги
# исчерпаны, окно достаётся другому аккаунту и в панели остаётся предупреждение
# (events). Паузами аккаунт не занимается.
#
# Лестница проходит ОДИН РАЗ на окно и ОДИН РАЗ на аккаунт за прогон: счётчики живут
# в empty_state на самом аккаунте. Раньше они сбрасывались на каждом заходе в
# work_account — аккаунт возвращался из отлёжки и снова жал F5, менял count и дробил
# окна: на 13 молча пустых окон уходило под сотню запросов, десяток открытий браузера
# и новые окна из дробления. Теперь после EMPTY_STRIKES_HOLD разных пустых окон
# аккаунт переходит в «тихий» режим: он берёт каждое окно ОДНИМ запросом, лестницу не
# повторяет и окна не дробит — они остаются в базе pending, их добьёт следующий
# «Старт». Тихий режим снимается, как только пришёл хоть один пост.
#
# «Тихий ноль» бывает и у ВСЕХ аккаунтов сразу — тогда лестница бесполезна: каждый
# шаг это лишний запрос к VK и лишняя строка в логе, а очередь выгорает без постов.
# Такой режим считаем отдельно, по всему прогону (см. EMPTY_GLOBAL_* ниже).
EMPTY_F5_RETRIES = 2       # столько F5 в профиле на аккаунт за прогон (не на окно)
EMPTY_F5_TIMEOUT_SEC = 60  # сколько ждать ленту в профиле, прежде чем менять запрос
EMPTY_RETRY_COUNT = 20     # count узкого повтора: то же окно, но другой запрос
# Столько смен адреса на аккаунт за прогон: F5 сессию освежил, а выдача та же — значит
# помечен не токен, а IP (прокси мог засветиться). Аккаунт получает свободный прокси
# из запаса (data/proxy_stock.txt, пополняется в панели на странице «Прокси»), прежний
# адрес возвращается в запас, окно берётся заново. Запаса нет — шаг пропускается.
EMPTY_PROXY_SWAPS_PER_RUN = 1
EMPTY_SPLITS_PER_RUN = 1   # столько окон за прогон делим пополам
EMPTY_SPLIT_MIN_SEC = 900  # окно уже этого не делим: иначе окна множатся по прогонам
EMPTY_STRIKES_HOLD = 3     # столько РАЗНЫХ пустых окон подряд — аккаунт в отлёжку
EMPTY_TOKEN_REFRESH_RETRIES = 2  # столько обновлений сессии на подряд идущие «token»
# «Тихий ноль — у всех»: VK молча режет выдачу сразу всем аккаунтам (тихое
# ограничение по адресу/сессии). Лестница (F5 → другой count → дробление → другому
# аккаунту) тут не помогает: каждый шаг — лишний запрос к VK и лишняя строка в
# логе, а очередь выгорает без постов. Поэтому РАЗНЫЕ пустые окна подряд считаем ПО
# ВСЕМ аккаунтам вместе (счётчик — общий на прогон, живёт между воркерами):
#   * после EMPTY_GLOBAL_QUIET окон — общий «тихий» режим: лестницу и запись в
#     панель по каждому окну выключаем, окно стоит ровно один запрос (одна строка
#     в логе на переход);
#   * после EMPTY_GLOBAL_STOP окон — раунд прерываем одной строкой в логе, а не
#     молотим очередь: недобитые окна остаются pending, их добьёт следующий заход,
#     когда ограничение отпустит (в режиме «Работать, ч» прогон сам ждёт и пробует
#     снова — EMPTY_GLOBAL_BACKOFF_SEC).
# Серия рвётся, как только пришёл хоть один пост.
EMPTY_GLOBAL_QUIET = 10    # столько разных пустых окон подряд по всем аккаунтам — общий «тихий» режим
EMPTY_GLOBAL_STOP = 40     # столько — раунд прерываем: VK режет выдачу всем аккаунтам
EMPTY_GLOBAL_BACKOFF_SEC = 300  # пауза перед новой попыткой, когда ВК режет всех, с
# Осколок периода короче этого не режем в отдельное окно: иначе «за 6 часов»
# рождало окно на 1–20 секунд (в логе «03:42–03:42»), которое ещё и дёргало VK.
MIN_FRAGMENT_SEC = 60


def empty_is_throttle(client, db: DB, job: dict, mode: Optional[str] = None) -> bool:
    """Пустое окно — правда пусто или ВК молча режет выдачу?

    1) в базе уже есть посты за это окно → точно ограничение: эти часы уже
       собирались, значит выдача по ним есть;
    2) иначе «канарейка»: час у самого свежего известного поста тега — он пустым
       не бывает. Канарейка молчит → сессия не отвечает, и пустота окна ничего не
       доказывает: считаем ограничением. Канарейка жива → сессия работает, и
       пустое окно принимаем за правду (в базе по этим часам постов нет).

    Глубину выдачи по «первому посту в базе» не угадываем: у «бровиста» первый пост
    был 12.10, а VK спокойно отдаёт 05.10 (45 постов на окно в 6 ч) — проверено на
    живой сессии. Поэтому старые окна не пропускаются, а спрашиваются как все.

    `mode` — чем искать тег сейчас (авто-режим: «hash»/«word»), иначе канарейка
    спросит не ту выдачу и «пусто» будет ложным.
    """
    tag, start, end = job["tag"], int(job["start_time"]), int(job["end_time"])
    if db.count_tag_posts_between(tag, start, end):
        return True
    latest = db.tag_max_date(tag)
    if not latest:
        return False  # тег новый, сравнить не с чем
    try:
        probe = client.search_hashtag(
            tag, count=10, start_time=latest - 3600, end_time=latest + 1, mode=mode
        )
    except Exception:  # noqa: BLE001 — ошибку канарейки считаем ограничением
        return True
    return not probe.get("items")


def empty_state(acc) -> dict:
    """Состояние «тихого нуля» на аккаунт за прогон (живёт между вызовами воркера).

    Держим его на самом аккаунте, а не внутри work_account: аккаунт возвращается из
    отлёжки и снова попадает в работу — счётчики обязаны остаться, иначе лестница
    (F5 → другой count → дробление) и отлёжка повторяются по кругу, а прогон молотит
    одни и те же пустые выдачи.
    """
    state = getattr(acc, "empty_state", None)
    if state is not None:
        return state
    state = {
        "strikes": 0,  # РАЗНЫХ пустых окон подряд — по нему аккаунт уходит в отлёжку
        "stuck": set(),  # окна, о которых предупреждение в панели уже написано
        "handed": set(),  # окна, уже отданные другому аккаунту
        "f5_left": EMPTY_F5_RETRIES,  # сколько F5 осталось на весь прогон
        "proxy_left": EMPTY_PROXY_SWAPS_PER_RUN,  # сколько смен адреса осталось
        "proxy_note": False,  # про «сменить нечем» на этом аккаунте уже сказали
        "splits_left": EMPTY_SPLITS_PER_RUN,  # сколько окон ещё можно поделить
        "quiet": False,  # «тихий» режим: лестницу не повторяем
        "held": False,  # отлёжка за пустые выдачи в этом прогоне уже была
    }
    try:
        acc.empty_state = state
    except AttributeError:  # у объекта нет своих атрибутов (заглушка в тестах)
        pass
    return state


_silent_lock = threading.Lock()
_silent_seen: set = set()  # разные пустые окна подряд по всему прогону (все аккаунты)

_silent = {"saw": 0, "quiet": False, "stop": False, "notified": False}


def silent_note_empty(strike_win) -> int:
    """Отмечает молча пустое окно по всему прогону. Возвращает, сколько таких подряд.

    Одно окно считаем один раз, даже если его переспрашивали (лестница, другой
    аккаунт): иначе счётчик раздувался бы повторами и рвал раунд слишком рано.
    Caller уже отбросил половинки окна — они приходят с ключом родителя.
    """
    with _silent_lock:
        if strike_win in _silent_seen:
            return int(_silent["saw"])
        _silent_seen.add(strike_win)
        _silent["saw"] = int(_silent["saw"]) + 1
        if _silent["saw"] >= EMPTY_GLOBAL_QUIET:
            _silent["quiet"] = True
        if _silent["saw"] >= EMPTY_GLOBAL_STOP:
            _silent["stop"] = True
        return int(_silent["saw"])


def silent_reset() -> None:
    """Пришёл пост (или начался новый прогон) — серия пустых выдач по прогону кончилась."""
    with _silent_lock:
        _silent_seen.clear()
        _silent["saw"] = 0
        _silent["quiet"] = False
        _silent["stop"] = False
        _silent["notified"] = False


def silent_quiet_once() -> bool:
    """True — про общий «тихий ноль» ещё не сообщали (в лог и панель пишем один раз).

    Аккаунтов несколько, и каждый замечает общий тормоз; без этой отметки в лог и
    панель ушла бы строка на каждый аккаунт.
    """
    with _silent_lock:
        if _silent["notified"]:
            return False
        _silent["notified"] = True
        return True


def silent_rearm() -> None:
    """Снимает «стоп раунда» после паузы, но «тихий» режим оставляет.

    Если VK всё ещё режет выдачу, первое же пустое окно снова упрётся в порог (см.
    silent_note_empty): прогон дёргает VK одним запросом за паузу, а не выжигает
    очередь заново.
    """
    with _silent_lock:
        _silent_seen.clear()
        _silent["stop"] = False
        _silent["saw"] = max(0, EMPTY_GLOBAL_STOP - 1)


def silent_quiet() -> bool:
    with _silent_lock:
        return bool(_silent["quiet"])


def silent_stopped() -> bool:
    with _silent_lock:
        return bool(_silent["stop"])


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt_ts(ts: int) -> str:
    # Секунды нужны: «за 6 часов» и «хвост периода» отличаются на десяток секунд,
    # и без них границы окон в логе выглядели как «03:42–03:42».
    return time.strftime("%d.%m %H:%M:%S", time.localtime(int(ts)))


def fmt_left(seconds: float) -> str:
    """«2 ч 15 мин» — сколько ещё работать (идёт в строку «осталось»)."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{total} с"


def duty_left(started_at: float, run_hours: float, now: Optional[float] = None) -> float:
    """Сколько секунд работы осталось (0 — время вышло или --run-hours выключен)."""
    if not run_hours or run_hours <= 0:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, started_at + float(run_hours) * 3600.0 - now)


def duty_round_waits(duty_hours: float, mark_new: int) -> bool:
    """Ждать ли «Пауза, с» перед следующим кругом «Работать, ч».

    Прошлый круг не принёс ни одного нового поста — повторять его сразу нечем:
    окно свежего хвоста у VK то же самое, ответ тот же, а каждое повторение это
    лишний запрос, лестница (F5, другой count, дробление) и новая строка окна в
    базе. Так и написано в README: «через «Пауза, с» проверка повторится».

    mark_new — счётчик новых постов `_stats["new"]`, снятый перед работой круга.
    Без «Работать, ч» (duty_hours = 0) поведение прежнее: один проход.
    """
    return bool(duty_hours) and int(_stats["new"]) <= int(mark_new)


def bump(**kwargs) -> None:
    with _stats_lock:
        for key, value in kwargs.items():
            _stats[key] = _stats.get(key, 0) + int(value)


def snapshot() -> dict:
    with _stats_lock:
        return dict(_stats)


def parse_tags_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("//") or (s.startswith("#") and s[1:2].isspace()):
            continue  # пустая строка или комментарий: «// заметка» и «# заметка»
        tag = s.lstrip("#").strip()
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


class JobQueue:
    """Очередь окон. Поток выходит, только когда очередь пуста И никто не
    держит окно в работе (иначе пропустим половинки от дробления)."""

    def __init__(self) -> None:
        self._items: deque = deque()
        self._cv = threading.Condition()
        self._active = 0

    def put(self, job: dict, front: bool = False) -> None:
        with self._cv:
            (self._items.appendleft if front else self._items.append)(job)
            self._cv.notify_all()

    def get(self) -> Optional[dict]:
        with self._cv:
            while True:
                if _stop_flag.is_set():
                    return None
                if self._items:
                    self._active += 1
                    return self._items.popleft()
                if self._active == 0:
                    return None
                self._cv.wait(timeout=0.5)

    def done(self) -> None:
        with self._cv:
            self._active -= 1
            self._cv.notify_all()

    def size(self) -> int:
        with self._cv:
            return len(self._items)

    def drained(self) -> bool:
        """Пусто и никто не держит окно — воркерам больше нечего делать."""
        with self._cv:
            return not self._items and self._active == 0


def build_pool(
    accounts: list[Account], db: DB, workers: int, proxy_override: str = ""
) -> tuple[AccountPool, int]:
    """Пул аккаунтов и число воркеров. Без accounts.txt — одна общая сессия.

    Пул вмещает ВСЕ аккаунты, а воркеров — сколько задано: упавший/ушедший в
    отлёжку аккаунт заменяется следующим живым, простой не копится.
    """
    if not accounts:
        items = [PoolAccount("session", proxy_override, SESSION_FILE)]
    else:
        items = load_pool_accounts(accounts, db, proxy_override)
    pool = AccountPool(items)
    alive = pool.alive()
    if not alive:
        return pool, 0
    threads = workers if workers and workers > 0 else alive
    return pool, max(1, min(threads, alive))


def describe_pool(pool: AccountPool) -> str:
    snap = pool.snapshot()
    ready = sum(1 for s in snap if s["status"] == READY)
    hold = sum(1 for s in snap if s["status"] == HOLD)
    dead = sum(1 for s in snap if s["status"] == DEAD)
    return f"аккаунтов {len(snap)} (готовы {ready}, отлёжка {hold}, мертвы {dead})"


def pool_has_ready(pool: AccountPool) -> bool:
    return any(s["status"] == READY for s in pool.snapshot())


def make_client(session_path: Path, proxy: str, pause: float, label: str):
    if not Path(session_path).exists():
        log(f"{label}: нет файла сессии {session_path} — вход: python src/vk_auth.py --login")
        return None
    try:
        return VkFeedClient.from_session(Path(session_path), proxy, pause=pause)
    except Exception as exc:  # noqa: BLE001
        log(f"{label}: не удалось создать клиент ({exc})")
        return None


def set_client_proxy(client, proxy: str) -> None:
    """Меняет прокси живому клиенту аккаунта (смена адреса на ходу).

    Клиент запоминает прокси при создании, а curl_cffi-сессия поднимается лениво:
    без этого вызова клиент продолжил бы ходить по старому адресу и проба смены
    прокси ничего бы не проверила. У заглушек в тестах метода нет — им хватает поля.
    """
    try:
        client.set_proxy(proxy)
    except AttributeError:  # заглушка вместо VkFeedClient
        client.proxy = proxy


_browser_lock = threading.Lock()  # браузеры обновления сессии идём по одному — меньше нагрузки и подозрений


def _show_browser_kwargs(show: bool) -> dict:
    """Аргумент headless для эмулятора браузера.

    Передаём его ТОЛЬКО когда окно показываем: без показа путь остаётся ровно
    прежним (вызов как был, без лишнего параметра).
    """
    return {"headless": False} if show else {}


def resolve_show_browser(args, settings: dict) -> tuple[bool, str]:
    """Показывать ли окно браузера: (флаг, причина отказа).

    Просят окно флагом --show-browser или настройкой "show_browser" (галочка в
    панели). Показываем его, только если браузер не выключен совсем (--no-browser)
    и есть графическая сессия. Второе значение — строка для лога о том, почему
    показать не вышло ("" — показываем или никто и не просил).

    Показ виден дважды: эмулятор обновляет сессию на виду (F5 на ленте, обмен
    токена) и отдельное окно показывает сам обход ленты (см. view_crawl_window).
    """
    if not bool(getattr(args, "show_browser", False) or (settings or {}).get("show_browser")):
        return False, ""
    if getattr(args, "no_browser", False):
        return False, "--no-browser выключает браузер совсем — показ окна не нужен"
    if not has_display():
        return False, (
            f"показывать окно браузера не могу: графической сессии нет ({os_label()}) — "
            "эмулятор работает скрыто (на сервере так и надо)"
        )
    return True, ""


def resolve_tag_query(args, settings: dict) -> str:
    """Чем искать тег в запросе к VK: "hash" (#тег), "word" (слово) или "auto".

    Просят его флагом --tag-query или настройкой "tag_query" — переключатель «с #» /
    «без #» / «авто» на странице «Теги» в панели. Флаг сильнее настройки; в args
    кладём уже готовое значение, чтобы все части прогона (воркеры, окно обхода)
    читали одно и то же без повторных разборов. У VK хештег и слово — две разные
    выдачи, поэтому «hash»/«word» — режим на весь прогон, а «auto» выбирает режим
    для каждого тега отдельно (tag_mode_for).
    """
    value = getattr(args, "tag_query", None)
    if value is None:
        value = (settings or {}).get("tag_query")
    return normalize_tag_query(value)


# Авто-режим тега (tag_query = "auto"): выбранное «хештег/слово» помним и в базе
# (tags.query_mode), и в памяти прогона. У тега несколько окон, а проба стоит двух
# запросов — поэтому она идёт один раз на тег, а не на каждое окно; в следующих
# прогонах режим берётся из базы и пробных запросов уже не делает.
_TAG_MODES: dict[str, str] = {}
_TAG_MODES_LOCK = threading.Lock()


def seed_tag_modes(db: DB) -> None:
    """Поднимает из базы уже выбранные авто-режимы тегов (см. tag_mode_for)."""
    with _TAG_MODES_LOCK:
        _TAG_MODES.clear()
        for row in db.all_tags():
            mode = str(row.get("query_mode") or "")
            if mode in ("hash", "word"):
                _TAG_MODES[str(row.get("query") or "")] = mode


def tag_mode_for(db: Optional[DB], client, tag: str, setting: str) -> str:
    """Чем искать ЭТОТ тег: настройка tag_query, а в «авто» — по факту выдачи VK.

    VK отдаёт по «#тег» и по «тег» РАЗНЫЕ выдачи. У нишевых тегов хештег почти
    пустой (люди пишут слово без решётки): прогон по хештегу выглядит как «0
    постов», хотя по слову лента не кончается. Поэтому в «авто» тег один раз
    проверяется двумя дешёвыми запросами (хештег и слово за последние сутки),
    выбирается тот, где постов больше, и выбор запоминается в базе: следующие
    прогоны пробных запросов не делают. Числа пишем в лог — видно, почему так.
    """
    setting = normalize_tag_query(setting)
    if setting != "auto":
        return setting
    key = extract_tag(tag)
    with _TAG_MODES_LOCK:
        known = _TAG_MODES.get(key)
    if known:
        return known
    try:
        mode, hash_items, word_items = detect_tag_mode(client, key, hours=MODE_PROBE_HOURS)
    except Exception as exc:  # noqa: BLE001 — проба не должна валить окно
        log(f"#{key}: режим тега не определился ({type(exc).__name__}: {exc}) — ищу хештегом")
        return "hash"
    if not hash_items and not word_items:
        log(f"#{key}: за {MODE_PROBE_HOURS} ч пусто и хештегом, и словом — ищу хештегом")
        return "hash"
    with _TAG_MODES_LOCK:
        _TAG_MODES[key] = mode
    if db is not None:
        try:
            db.set_tag_query_mode(key, mode)
        except Exception as exc:  # noqa: BLE001 — база занята: режим проживёт до конца прогона
            log(f"#{key}: режим не записался в базу ({exc}) — запомню до конца прогона")
    log(
        f"#{key}: за {MODE_PROBE_HOURS} ч хештегом {hash_items}, словом {word_items} — "
        f"ищу {'словом' if mode == 'word' else 'хештегом'}"
    )
    return mode


# Окно на виду во время самого обхода (--show-browser / show_browser). Обход постов
# идёт HTTP-запросами (newsfeed.search), поэтому в браузере его не видно: окно
# открывает ту же выдачу по тегу, что берёт прогон (#тег или слово — как в tag_query),
# и подписывает её — какое окно времени обходится прямо сейчас. Окно одно на прогон,
# живёт в своём потоке и в своём браузере (профиль не занимает), обход его не ждёт:
# показывается последнее окно. При этом F5 окно не жмёт вообще — окна времени
# сменяются прокруткой ленты, чтобы посты успевали прогрузиться (см. FeedViewer).
# Если окно не поднялось (нет экрана, нет браузера), обход идёт как обычно,
# а причина — одной строкой в логе.
_viewer_lock = threading.Lock()
_viewer: Optional[Any] = None


def start_feed_viewer(pool: AccountPool, args, settings: dict) -> Optional[Any]:
    """Окно обхода: поднимает его один раз за прогон. None — показывать нечего.

    Аккаунт для окна — первый годный из пула: окно остаётся на нём (свои cookies,
    свой прокси), а какие окна времени обходить — подсказывает сам прогон. Режим
    тега окно берёт из того же tag_query, что и запросы прогона, — тогда в окне
    видна ровно та выдача, которую собираем (#тег или слово без решётки).
    """
    show, _why = resolve_show_browser(args, settings)
    if not show:
        return None
    with _viewer_lock:
        global _viewer
        if _viewer is not None and _viewer.alive():
            return _viewer
        acc = next((a for a in pool.items() if a.status != DEAD and a.session_path), None)
        if acc is None:
            return None
        from vk_auth import FeedViewer

        mode = resolve_tag_query(args, settings)
        _viewer = FeedViewer(
            Path(acc.session_path), acc.proxy, label=acc.login, tag_query=mode
        ).start()
        log(
            f"окно на виду: показываю обход лентой аккаунта {acc.login} — в окне та же "
            f"выдача по тегу, что берёт прогон ({TAG_QUERY_LABELS[mode]}); если VK не "
            "примет cookies, окно скажет об этом само"
        )
        return _viewer


def view_crawl_window(
    pool: AccountPool,
    args,
    settings: dict,
    job: dict,
    lo: int,
    hi: int,
    label: str = "",
    mode: Optional[str] = None,
) -> None:
    """Показать в окне то окно времени, которое обход берёт прямо сейчас.

    Зовётся на каждом окне перед запросом к VK. Ничего не ждёт и никогда не
    бросает исключений: окно — это подсказка глазами, а не часть обхода, и сбой
    окна не должен отмечать окно времени сбоем.

    `mode` — чем ищется ЭТОТ тег (авто-режим: «hash»/«word»): окно показывает ту
    же выдачу, что собирает прогон, поэтому режим тега ему тоже передаём.
    """
    try:
        if int(lo) >= int(hi):
            return  # вырожденное окно (смотреть нечего)
        viewer = start_feed_viewer(pool, args, settings)
        if viewer is None:
            return
        tag = str(job.get("tag") or "")
        mode = normalize_tag_query(mode or resolve_tag_query(args, settings))
        if str(getattr(viewer, "tag_query", "")) != mode:
            # Сменился тег — мог смениться и режим: окно должно открыть ту же
            # выдачу, что уйдёт в запрос (иначе в окне «не та» лента).
            setattr(viewer, "tag_query", mode)
        # В подписи — тот же тег, что ушёл в запрос: с решёткой или словом без неё.
        shown = f"{tag} (словом)" if mode == "word" else f"#{tag}"
        caption = (
            f"обход ленты: {shown}  {fmt_ts(lo)}–{fmt_ts(hi)}  "
            f"окно #{job.get('id')}  {label}"
        ).strip()
        viewer.visit(tag, caption)
    except Exception as exc:  # noqa: BLE001 — окно не срывает обход
        log(f"окно на виду: показать окно обхода не вышло ({exc}) — обход идёт как обычно")


def stop_feed_viewer() -> None:
    """Закрывает окно обхода: прогон кончился («Стоп», Ctrl+C или окна собраны)."""
    with _viewer_lock:
        global _viewer
        viewer, _viewer = _viewer, None
    if viewer is not None:
        viewer.stop()


def _refresh_client(client, session_path: Path, proxy: str, label: str, show: bool = False) -> bool:
    """Новый токен: сначала профиль браузера (лента через F5 — работает и когда ВК
    режет обмен cookies ошибкой «too much tries»), затем по cookies. True — клиент жив.

    show=True — окно эмулятора на виду (--show-browser / show_browser): видно, как
    он заходит в ленту и жмёт F5.
    """
    from vk_auth import refresh_from_cookies, refresh_via_browser

    attempts = (
        ("профиль браузера (F5)", lambda: refresh_via_browser(
            Path(session_path), proxy, **_show_browser_kwargs(show))),
        ("cookies", lambda: refresh_from_cookies(Path(session_path), proxy)),
    )
    for name, fn in attempts:
        try:
            if name == "профиль браузера (F5)":
                with _browser_lock:
                    payload = fn()
            else:
                payload = fn()
        except Exception as exc:  # noqa: BLE001
            log(f"{label}: токен через {name} не обновлён ({exc})")
            continue
        token = str(payload.get("access_token") or "")
        if token:
            client.token = token
            client.api_version = payload.get("api_version") or client.api_version
            log(f"{label}: токен обновлён через {name}")
            return True
    return False


def _f5_refresh(client, session_path: Path, proxy: str, label: str, show: bool = False) -> bool:
    """Освежает сессию через ленту в профиле браузера (F5) — без полного входа.

    Так лечится «ВК молча отдаёт пустую выдачу»: сервер режет API сессии, которую
    считает старой, и пустая выдача — это не «постов нет», а тихий отказ. Обычный
    заход в ленту из живого профиля сессию освежает: VK выдаёт свежие cookies, а по
    ним берётся новый web_token. Пауза этого не делает — поэтому сначала F5, и только
    если он не помог, ждём. True — клиент продолжил работу новым токеном.

    show=True — окно эмулятора на виду (--show-browser / show_browser): браузер
    открывается видимым и обновляет ленту на глазах.
    """
    from vk_auth import token_via_feed_reload

    try:
        with _browser_lock:  # браузеры идут по одному
            payload = token_via_feed_reload(
                Path(session_path), proxy, timeout=EMPTY_F5_TIMEOUT_SEC,
                **_show_browser_kwargs(show),
            ) or {}
    except Exception as exc:  # noqa: BLE001 — нет профиля, капча, прокси, «too much tries»
        log(f"{label}: F5 в профиле не помог ({exc})")
        return False
    token = str(payload.get("access_token") or "")
    if not token:
        log(f"{label}: F5 в профиле не дал токен")
        return False
    client.token = token
    if payload.get("api_version"):
        client.api_version = payload["api_version"]
    log(f"{label}: F5 в профиле — сессия освежена, клиент работает новым токеном")
    # Дальше этот аккаунт обновляем тем же путём: HTTP-обмен у VK притормозился.
    prefer_browser(label)
    return True


_live_lock = threading.Lock()
_live_clients: dict[str, object] = {}


def register_client(login: str, client) -> None:
    """Запоминает живой клиент аккаунта.

    Нужно фону обновления сессии: он меняет токен аккаунта на свежий и должен
    подставить его тому самому клиенту, которым воркер уже работает, — иначе
    клиент остался бы со старым токеном до следующей ошибки «token».
    """
    with _live_lock:
        _live_clients[str(login)] = client


def forget_client(login: str, client=None) -> None:
    """Забывает клиент аккаунта: воркер отпустил аккаунт в пул."""
    with _live_lock:
        if client is None or _live_clients.get(str(login)) is client:
            _live_clients.pop(str(login), None)


def apply_token(login: str, token: str, api_version: str = "") -> int:
    """Подставляет свежий токен живым клиентам аккаунта.

    Возвращает, скольким клиентам подставили: по этому числу в логе видно, что
    обновление дошло до работы, а не осталось только в файле сессии.
    """
    if not token:
        return 0
    with _live_lock:
        targets = [obj for key, obj in _live_clients.items() if not login or key == login]
    for client in targets:
        client.token = token
        if api_version:
            client.api_version = api_version
    return len(targets)


# Фон обновления сессии. Токен VK живёт ограниченное время (в файле сессии —
# token_expires_at, обычно около суток), а прогон «за 30 суток» идёт часами:
# раньше токен умирал посреди работы, аккаунты выбывали («нужен повторный вход»),
# и очередь ждала ручного входа.
MIN_REFRESH_INTERVAL_SEC = 30.0  # чаще раза в полминуты VK дёргать не стоит
REFRESH_FAIL_RETRY_SEC = 60.0  # обмен не удался — раньше минуты не повторяем
REFRESH_FAIL_RETRY_MAX_SEC = 600.0  # ...и позже десяти минут тоже (чтобы успеть до срока)
REFRESH_THROTTLE_BACKOFF_SEC = 300.0  # «too much tries» — VK сам притормозил обмен
REFRESH_EXPIRY_MARGIN_SEC = 900.0  # за 15 мин до истечения обновляем, не ждём срока
REFRESH_LOG_EVERY_SEC = 600.0  # успехи пишем в лог раз в 10 мин на аккаунт
# Признаки троттлинга в отказе обмена. Раньше на них фон вообще не открывал браузер
# («упрётся в то же ограничение»), и сессия не обновлялась, пока VK режет HTTP. Теперь
# наоборот: троттлинг — повод уйти на профиль браузера с F5 (там для VK это обычный
# заход в ленту), а эти же строки задают паузу до повторной попытки (см. _retry_in).
THROTTLE_HINTS = ("too much tries", "flood", "error 6", "error 9")
# Сколько держать аккаунт на браузерном обновлении (лента + F5), если VK притормозил
# HTTP-обмен: полчаса хватает, чтобы ограничение отпустило, а прогон не голодал.
BROWSER_PREFER_SEC = 1800.0

# До какого времени аккаунт обновляем лентой в профиле браузера (F5), а не HTTP-обменом.
# Список один на всех: и фон обновления сессии, и воркеры. Воркер, освеживший сессию
# через F5 после пустой выдачи, тем самым переводит аккаунт на этот путь и в фоне —
# HTTP-обмен у VK притормозился, и долбить его смысла нет.
_browser_prefer_until: dict[str, float] = {}
_browser_prefer_lock = threading.Lock()


def prefer_browser(login: str, seconds: float = BROWSER_PREFER_SEC) -> None:
    """Помечает аккаунт: сессию обновляем лентой в профиле браузера (F5), а не HTTP."""
    if not login:
        return
    with _browser_prefer_lock:
        _browser_prefer_until[str(login)] = max(
            _browser_prefer_until.get(str(login), 0.0), time.time() + float(seconds)
        )


def browser_preferred(login: str) -> bool:
    """Обновлять ли сессию этого аккаунта браузером (лента + F5) вместо HTTP-обмена."""
    with _browser_prefer_lock:
        return time.time() < _browser_prefer_until.get(str(login), 0.0)


def session_refresh_minutes(args, settings: dict) -> float:
    """Сколько минут между обновлениями сессии. 0 — не обновлять.

    Флаг прогона важнее настройки: `--session-refresh-minutes 5` меняет интервал
    на один запуск, `--no-session-refresh` выключает обновление совсем.
    """
    if args.no_session_refresh:
        return 0.0
    if args.session_refresh_minutes:
        return max(0.0, float(args.session_refresh_minutes))
    return max(0.0, float(settings.get("session_refresh_minutes") or 0))


class SessionRefresher(threading.Thread):
    """Меняет access_token аккаунтов, пока идёт прогон (фоновый поток).

    Раз в session_refresh_minutes берёт из пула живые аккаунты и обменивает их cookies
    на свежий токен, после чего подставляет токен работающим клиентам. Первым идёт
    дешёвый HTTP-обмен cookies; не вышло — открываем профиль браузера, обновляем ленту
    через F5 и берём cookies уже из живого браузера (по одному). Если VK
    притормозил HTTP-обмен («too much tries»), этот аккаунт дальше обновляется лентой
    в профиле браузера: HTTP будет упираться в то же ограничение, а заход в ленту —
    нет. Совсем ничего не вышло: старый токен остаётся как есть, попытка повторяется
    позже, причина отказа — в логе.

    show_browser=True — окно этого браузера на виду (настройка show_browser /
    --show-browser): видно, что эмулятор делает с лентой. Окна всё равно идут по
    одному (_browser_lock), а не по числу воркеров.
    """

    def __init__(
        self,
        pool: AccountPool,
        settings: dict,
        no_browser: bool = False,
        show_browser: bool = False,
    ) -> None:
        super().__init__(name="session-refresh", daemon=True)
        self.pool = pool
        self.no_browser = no_browser
        self.show_browser = show_browser
        self.interval = max(
            MIN_REFRESH_INTERVAL_SEC, float(settings.get("session_refresh_minutes") or 0) * 60
        )
        self.done = 0  # сколько обменов прошло успешно
        self._next_at: dict[str, float] = {}
        self._logged_at: dict[str, float] = {}
        self._count: dict[str, int] = {}
        # до какого времени аккаунт обновляем лентой в браузере (F5), а не HTTP-обменом;
        # список общий с воркерами (см. prefer_browser) — удачный F5 у воркера тоже
        # переводит аккаунт на этот путь
        self._prefer_browser_until = _browser_prefer_until

    def accounts(self) -> list[PoolAccount]:
        """Кого обновлять. У мёртвых нет годных cookies — им нужен вход."""
        return [a for a in self.pool.items() if a.status != DEAD and a.session_path]

    def run(self) -> None:
        while not _stop_flag.wait(self.interval):
            for acc in self.accounts():
                if _stop_flag.is_set():
                    return
                self.refresh_due(acc)

    def refresh_due(self, acc: PoolAccount) -> Optional[bool]:
        """Обновляет сессию, если пришёл срок. None — срок не пришёл."""
        now = time.time()
        expires_at = float(self._session(acc).get("token_expires_at") or 0)
        margin = min(self.interval, REFRESH_EXPIRY_MARGIN_SEC)
        # Срок пришёл, если прошёл интервал — или если токен вот-вот истечёт:
        # интервал может быть длиннее остатка жизни токена, тогда обновляем раньше.
        soon = bool(expires_at) and expires_at - now <= margin
        if not soon and now < self._next_at.get(acc.login, 0.0):
            return None
        ok, detail = self._exchange(acc)
        retry_in = self._retry_in(detail) if not ok else self.interval
        self._next_at[acc.login] = time.time() + retry_in
        if ok:
            self.done += 1
        self._note(acc.login, ok, detail, retry_in)
        return ok

    def _retry_in(self, detail: str) -> float:
        """Через сколько вернуться после отказа.

        «too much tries» — VK сам притормозил обмен: отступаем на 5 минут, чтобы
        не долбить его каждую минуту. Прочие отказы (сеть, прокси, cookies)
        пробуем не чаще обычного интервала и не реже минуты. Токен около срока
        годности обновляется вне очереди — на это _next_at не влияет.
        """
        if self._throttled(detail):
            return REFRESH_THROTTLE_BACKOFF_SEC
        return min(max(REFRESH_FAIL_RETRY_SEC, self.interval), REFRESH_FAIL_RETRY_MAX_SEC)

    @staticmethod
    def _throttled(reason: str) -> bool:
        low = (reason or "").lower()
        return any(hint in low for hint in THROTTLE_HINTS)

    def _session(self, acc: PoolAccount) -> dict:
        try:
            from vk_auth import load_session

            return load_session(Path(acc.session_path)) or {}
        except Exception as exc:  # noqa: BLE001 — файл могли не дописать
            log(f"{acc.login}: файл сессии не прочитан ({exc})")
            return {}

    def _exchange(self, acc: PoolAccount) -> tuple[bool, str]:
        """Свежий токен: обмен cookies, а при отказе — профиль браузера с F5.

        Сначала дешёвый HTTP-обмен cookies; если он не прошёл — открываем профиль
        браузера, обновляем ленту через F5 и берём cookies из живого браузера. Когда
        VK притормозил HTTP-обмен («too much tries»), профиль становится основным
        путём для этого аккаунта на BROWSER_PREFER_SEC: HTTP будет упираться в то же
        ограничение, а заход в ленту браузером — нет. Обновлённый токен уходит живым
        клиентам, чтобы воркеры продолжили работу новым токеном.
        """
        from vk_auth import refresh_from_cookies

        prefer = browser_preferred(acc.login)
        steps = ("browser", "cookies") if prefer else ("cookies", "browser")
        errors: list[str] = []
        for step in steps:
            if step == "browser":
                if self.no_browser:
                    errors.append("профиль браузера (F5): выключен --no-browser")
                    continue
                try:
                    payload = self._via_browser(acc) or {}
                except Exception as exc:  # noqa: BLE001 — нет профиля, капча, прокси
                    errors.append(f"профиль браузера (F5): {exc}")
                    continue
                source = "профиль браузера (F5)"
            else:
                try:
                    payload = refresh_from_cookies(Path(acc.session_path), acc.proxy) or {}
                except Exception as exc:  # noqa: BLE001 — сеть, прокси, «too much tries»
                    # В тексте ошибки Playwright печатает запрос с cookie сессии.
                    errors.append(f"cookies: {redact(str(exc))}")
                    if self._throttled(str(exc)):
                        # VK режет чистый HTTP-обмен: дальше этот аккаунт обновляем
                        # лентой в профиле браузера (F5) — она идёт мимо ограничения.
                        prefer_browser(acc.login)
                    continue
                source = "cookies"
            token = str(payload.get("access_token") or "")
            if not token:
                errors.append(f"{source}: пустой токен")
                continue
            patched = apply_token(acc.login, token, str(payload.get("api_version") or ""))
            return True, f"{source}, клиентов {patched}"
        return False, "; ".join(errors)

    def _via_browser(self, acc: PoolAccount) -> dict:
        from vk_auth import refresh_via_browser

        with _browser_lock:  # браузеры идут по одному
            return refresh_via_browser(
                Path(acc.session_path),
                acc.proxy,
                **_show_browser_kwargs(self.show_browser),
            )

    def _note(self, login: str, ok: bool, detail: str, retry_in: float = REFRESH_FAIL_RETRY_SEC) -> None:
        """Успех — раз в 10 минут на аккаунт (иначе лог тонет), отказ — всегда."""
        now = time.time()
        if not ok:
            hint = ""
            if self._throttled(detail):
                hint = " (VK притормозил обмен"
                hint += ", профиль браузера с F5 тоже не помог)" if "F5" in detail else ")"
            log(
                f"{login}: сессию обновить не вышло ({detail}){hint} — "
                f"следующая попытка через {retry_in:.0f} с"
            )
            return
        self._count[login] = self._count.get(login, 0) + 1
        if now - self._logged_at.get(login, 0.0) < REFRESH_LOG_EVERY_SEC:
            return
        self._logged_at[login] = now
        times = self._count[login]
        tail = f"; всего {times} обменов" if times > 1 else ""
        log(
            f"{login}: сессия обновлена ({detail}{tail}) — токен меняю каждые "
            f"{self.interval / 60:.1f} мин"
        )


def split_windows(
    start: int, end: int, size_sec: int, min_sec: int = MIN_FRAGMENT_SEC
) -> list[tuple[int, int]]:
    """[start, end) → окна по size_sec, самые свежие первыми.

    Осколок короче min_sec не выделяем в отдельное окно, а приращиваем к соседнему
    (свежему): период [now-6ч, now] почти всегда длиннее ровно 6 часов на десяток
    секунд, и раньше из-за этого рождалось окно вроде «03:42–03:42» — лишний
    запрос к VK и непонятная строка в логе.
    """
    out: list[tuple[int, int]] = []
    hi = end
    while hi > start:
        lo = max(start, hi - size_sec)
        if out and hi - start < max(1, int(min_sec)):
            # Остаток короче минуты: это не окно, а хвост — приращиваем его к
            # предыдущему окну (оно как раз начинается с lo).
            out[-1] = (start, out[-1][1])
            break
        out.append((lo, hi))
        hi = lo
    return out


def split_job(db: DB, job: dict, queue: JobQueue, min_sec: int) -> bool:
    """Делит окно пополам, если выдача обрезана лимитом (~60 постов на запрос).

    Делим по времени, а не «ниже самого старого поста»: VK не гарантирует
    порядок выдачи, а половины добирают всё. False — мельче min_sec не делим.
    """
    start, end = int(job["start_time"]), int(job["end_time"])
    if end - start < 2 * min_sec:
        return False
    mid = start + (end - start) // 2
    db.update_job(job["id"], status="split", cursor="")
    root_key = job.get("root_key") or (job["tag"], start, end)
    for lo, hi in ((start, mid), (mid, end)):  # (mid, end) окажется первым
        child = db.add_job(job["tag"], lo, hi, job["round"])
        # Половинки — это то же самое окно: для отлёжки аккаунта они считаются
        # одним окном (root_key), а дробить их дальше нельзя — иначе одно
        # застрявшее окно делилось бы снова и снова и съедало прогон.
        child["root"] = False
        child["root_key"] = root_key
        queue.put(child, front=True)
    return True


def handle_result(db: DB, job: dict, res: dict, queue: JobQueue, settings: dict, label: str) -> str:
    """Решает судьбу окна. Возвращает 'ok' | 'stop' (слот больше не работает)."""
    tag = job["tag"]
    window = f"#{tag} {fmt_ts(job['start_time'])}–{fmt_ts(job['end_time'])}"
    bump(pages=res["pages"], new=res["new"])
    bump(dupes=int(res.get("dupes") or 0), junk=int(res.get("junk") or 0))
    # Окно уже было закрыто — это перепроверка (свежий хвост или --full): счётчики
    # берём заново. Иначе старые значения копились бы с каждым повтором, «seen»
    # переваливал бы за порог деления и окно навсегда теряло статус «собрано».
    rewalk = str(job.get("status") or "") == "done"
    stats = {
        "seen": res["seen"] if rewalk else int(job.get("seen") or 0) + res["seen"],
        "new": res["new"] if rewalk else int(job.get("new") or 0) + res["new"],
        "pages": res["pages"] if rewalk else int(job.get("pages") or 0) + res["pages"],
    }

    if res["status"] == "ok":
        min_sec = int(settings["min_window_minutes"]) * 60
        # Перепроверку закрытого окна не дробим: половинки ушли бы с границами
        # записанного окна (снова за пределы периода) и наплодили бы новых строк,
        # а хвост всё равно перепроверяется каждым «Стартом».
        if res["need_split"] and not rewalk and split_job(db, job, queue, min_sec):
            bump(split=1)
            log(f"{label}: {window}: получено {res['seen']} — делю окно пополам")
            return "ok"
        db.update_job(job["id"], status="done", cursor="", reason=res["reason"], **stats)
        bump(jobs=1)
        log(f"{label}: {window}: постов {stats['seen']}, новых {stats['new']} ({res['reason']})")
        return "ok"

    reason = res["reason"]
    bump(errors=1)
    db.event("error", "slot", f"{label}: {window} — {reason}", account=label, tag=tag)
    # Курсор пишем в базу: без этого окно после сбоя начинается с нуля и
    # заново выкачивает уже собранные страницы.
    kept_cursor = res.get("cursor") or job.get("cursor") or ""
    db.update_job(job["id"], status="pending", reason=reason, cursor=kept_cursor, **stats)
    job.update(stats, cursor=kept_cursor)
    if reason in ("token", "captcha"):
        queue.put(job, front=True)  # окно отдаём живому аккаунту
        return "stop"
    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    if attempts >= int(settings["job_attempts"]):
        db.update_job(job["id"], status="failed", attempts=attempts)
        log(f"{label}: {window}: окно брошено после {attempts} попыток ({reason})")
    else:
        db.update_job(job["id"], attempts=attempts)
        log(f"{label}: {window}: ошибка {reason}, повтор {attempts}/{settings['job_attempts']}")
        queue.put(job)
    return "ok"


def save_account_state(db: DB, acc: PoolAccount, outcome: str, reason: str) -> None:
    """Пишет в базу статус аккаунта после работы воркера.

    ok и empty — успех (аккаунт цел, очередь просто опустела).
    silent_stop — тоже не сбой аккаунта: раунд прервал общий «тихий ноль» (VK режет
    выдачу всем), аккаунт жив и продолжит в следующем заходе.
    """
    if outcome in ("ok", "empty", "silent_stop"):
        return
    if outcome == "hold":
        db.mark_account(acc.login, proxy=acc.proxy, status=HOLD, error=reason,
                        cooldown_until=acc.cooldown_until, bump_fail=True)
    else:
        db.mark_account(acc.login, proxy=acc.proxy, status=DEAD, error=reason, bump_fail=True)


def run_worker(pool: AccountPool, queue: JobQueue, args, settings: dict, db: DB) -> None:
    """Один поток: берёт аккаунт из пула и добивает очередь им.

    Аккаунт выбыл (отлёжка/мёртв) — берём из пула следующий.
    Очередь опустела — работа закончена.
    """
    while not queue.drained():
        acc = pool.acquire(timeout=8)
        if acc is None:
            if pool.alive() == 0 or queue.drained():
                break
            continue
        client = make_client(
            acc.session_path, acc.proxy, float(settings["request_pause_sec"]), acc.login
        )
        if client is None:
            pool.release(acc, "dead", "нет сессии/токена")
            if not args.dry_run:
                db.mark_account(acc.login, proxy=acc.proxy, status=DEAD,
                                error="нет сессии/токена", bump_fail=True)
            continue
        # Регистрируем клиент: фоновое обновление сессии подставит ему свежий токен.
        register_client(acc.login, client)
        outcome, reason = work_account(client, acc, queue, args, settings, db, pool)
        forget_client(acc.login, client)
        # «empty» (очередь опустела) и «silent_stop» (ВК молча режет выдачу всем
        # аккаунтам — раунд прерван, окна остались pending) — не сбой аккаунта:
        # без cooldown.
        pool.release(acc, "ok" if outcome in ("empty", "silent_stop") else outcome, reason)
        if not args.dry_run:
            save_account_state(db, acc, outcome, reason)
        if outcome == "hold":
            log(f"{acc.login}: отлёжка («{reason}») — беру следующий аккаунт")
        elif outcome == "dead":
            log(f"{acc.login}: мёртв («{reason}») — беру следующий аккаунт")
        if outcome in ("empty", "silent_stop"):
            break


def fetch_bounds(job: dict, args) -> tuple[int, int]:
    """Границы запроса к VK для окна.

    Перепроверка уже закрытого окна (status = 'done') качается строго внутри
    запрошенного периода: окно могло быть записано раньше и вылезать за его
    начало (в базе лежат окна шириной до window_hours), а «за 6 часов» собирает
    только эти 6 часов. Незакрытые окна берём как есть — иначе недобранный кусок
    остался бы в базе помеченным как собранный.
    """
    lo, hi = int(job["start_time"]), int(job["end_time"])
    if str(job.get("status") or "") != "done":
        return lo, hi
    period_lo = int(args.start_time or 0)
    period_hi = int(args.end_time or 0)
    lo = max(lo, period_lo) if period_lo else lo
    hi = min(hi, period_hi) if period_hi else hi
    return (lo, hi) if hi > lo else (int(job["start_time"]), int(job["end_time"]))


def work_account(
    client, acc: PoolAccount, queue: JobQueue, args, settings: dict, db: DB, pool: AccountPool
) -> tuple[str, str]:
    """Крутит окна одним аккаунтом. Исход: empty (очередь пуста) / hold / dead."""
    label, proxy, session_path = acc.login, acc.proxy, acc.session_path
    empty = empty_state(acc)  # счётчики «тихого нуля» — на аккаунт и весь прогон
    # Показывать окно эмулятора (--show-browser / show_browser): видно, как браузер
    # обновляет ленту через F5 и берёт токен, а окно обхода показывает само окно
    # времени. Причину отказа уже сказал main — здесь только итоговый флаг.
    show = resolve_show_browser(args, settings)[0]
    token_refreshes = 0  # сколько раз подряд обновляли сессию на ошибку «token»
    # args.count = 0 значит «брать из настроек» (так же, как в probe.main).
    count_req = int(args.count or settings["page_count"])
    while True:
        job = queue.get()
        if job is None:
            return ("empty", "очередь пуста")
        try:
            db.update_job(job["id"], status="running")
            fetch_lo, fetch_hi = fetch_bounds(job, args)
            # Авто-режим: этим тегом ищем так, как решила проба (или как уже записано
            # в базе) — один тег может идти словом, другой хештегом.
            mode = tag_mode_for(
                db, client, job["tag"], str(settings.get("tag_query") or "hash")
            )
            # Окно на виду (--show-browser / show_browser): глазами видно то же окно
            # времени, которое берёт обход, — обход идёт HTTP-запросами, и в браузере
            # его не видно. На сам запрос это не влияет и его не задерживает.
            view_crawl_window(pool, args, settings, job, fetch_lo, fetch_hi, label, mode)
            res = probe_mod.run_window(
                client, None if args.dry_run else db, job["tag"],
                start_time=fetch_lo, end_time=fetch_hi,
                count=int(job.get("empty_count") or count_req), max_pages=args.pages,
                cursor=job.get("cursor") or "",
                stop_when_no_new=int(settings["stop_when_no_new_pages"]),
                dry_run=args.dry_run, round_no=int(job["round"]),
                seen_before=int(job.get("seen") or 0),
                split_if_seen=int(settings["split_if_seen"]),
                mode=mode,
                on_page=lambda cur, _n, _f, jid=job["id"]: db.update_job(jid, cursor=cur),
            )
            win = f"#{job['tag']} {fmt_ts(job['start_time'])}–{fmt_ts(job['end_time'])}"
            if res["status"] == "error" and res["reason"] == "token":
                bump(token=1)
                token_refreshes += 1
                db.update_job(job["id"], status="pending", reason="token")
                if token_refreshes > EMPTY_TOKEN_REFRESH_RETRIES:
                    # Свежий токен VK тоже не принял: сессию обновляют по кругу, а окно
                    # стоит на месте. Считаем аккаунт выбывшим, окно добирают другие.
                    db.event("error", "slot",
                             f"{label}: {win} — сессия не ожила и после обновления, "
                             "нужен повторный вход", account=label, tag=job["tag"])
                    log(f"{label}: токен не принят и после {token_refreshes - 1} обновлений "
                        "сессии — аккаунт выбывает, очередь добирают другие")
                    queue.put(job, front=True)
                    return ("dead", "нужен повторный вход")
                if _refresh_client(client, session_path, proxy, label, show=show):
                    queue.put(job, front=True)  # тот же аккаунт, свежий токен
                    continue
                queue.put(job, front=True)  # окно достанется живому аккаунту
                return ("dead", "нужен повторный вход")
            if res["status"] == "error" and res["reason"] == "captcha":
                bump(captcha=1)
                db.update_job(job["id"], status="pending", reason="captcha")
                queue.put(job, front=True)
                return ("hold", "капча от VK")
            if res["status"] == "ok" and res["seen"] == 0 and not args.dry_run and \
                    empty_is_throttle(client, db, job, mode):
                # ВК вернул пусто, а посты по этим часам есть, — тихий ноль на запросе.
                # Пауза ответ не меняет, поэтому меняем сам запрос: свежая сессия (F5) →
                # другой count → окно пополам. Лестница идёт один раз на окно и один раз
                # на аккаунт за прогон: на половинках окна и после отлёжки она не
                # повторяется — иначе прогон молотит одни и те же пустые выдачи.
                bump(throttle=1)
                db.update_job(job["id"], status="pending", reason="empty_silent")
                root_win = job.get("root", True)  # половинки окна — root=False
                # Половинки — то же окно, что и их родитель: и в панели, и в отлёжке
                # считаем его один раз.
                strike_win = job.get("root_key") or \
                    (job["tag"], int(job["start_time"]), int(job["end_time"]))
                # Тот же расчёт — по всему прогону: VK молча режет выдачу сразу всем
                # аккаунтам. Тогда лестница бесполезна (каждый шаг — лишний запрос к VK
                # и лишняя строка в логе), а очередь выгорает без постов.
                seen_global = silent_note_empty(strike_win)
                quiet = empty["quiet"] or silent_quiet()
                if quiet:
                    empty["quiet"] = True  # этот аккаунт тоже в «тихом» режиме
                    if silent_quiet_once():
                        db.event("warn", "slot",
                                 f"ВК молча отдаёт пустую выдачу на {seen_global} разных окнах "
                                 "подряд по всем аккаунтам: лестницу выключаю, окна беру "
                                 "одним запросом", account=label, tag=job["tag"])
                        log(f"ВК молча отдаёт пустую выдачу на {seen_global} разных окнах "
                            "подряд по всем аккаунтам — лестницу выключаю, окна беру одним "
                            "запросом")
                if silent_stopped():
                    # Очередь выгорает без постов: окно возвращаем в очередь (оно
                    # останется pending), а раунд прерываем одной строкой — см. main.
                    queue.put(job, front=True)
                    return ("silent_stop", "ВК молча режет выдачу всем аккаунтам")
                # F5 не помог (или недоступен: --no-browser, бюджет израсходован,
                # общий «тихий» режим) — значит помечен не токен, а адрес: VK молча
                # режет выдачу конкретному IP. Берём аккаунту свободный прокси из
                # запаса (data/proxy_stock.txt, пополняется в панели на странице
                # «Прокси»), прежний адрес возвращаем в запас и берём окно заново уже
                # с нового адреса. Смена одна на аккаунт за прогон: она правит
                # accounts.txt, поэтому дороже остальных шагов лестницы.
                # В общем «тихом» режиме (VK режет выдачу всем аккаунтам) лестницу не
                # повторяем, но одну пробу адреса делаем: если дело в помеченных
                # прокси, только смена адреса и поможет.
                f5_ready = bool(empty["f5_left"]) and not args.no_browser
                if root_win and not args.dry_run and not job.get("empty_proxy_swap") and \
                        empty["proxy_left"] and not args.no_proxy_swap and \
                        (quiet or job.get("empty_f5_done") or not f5_ready):
                    swapped = swap_account_proxy(label, proxy)
                    if swapped.get("ok"):
                        job["empty_proxy_swap"] = True
                        empty["proxy_left"] -= 1
                        proxy = str(swapped["new"])
                        acc.proxy = proxy  # в базе, в отлёжке и в фоне — новый адрес
                        set_client_proxy(client, proxy)  # и в живом HTTP-клиенте
                        db.mark_account(label, proxy=proxy, status=READY, error="")
                        db.event("warn", "slot",
                                 f"{label}: {win} молча пусто и после F5 — меняю прокси "
                                 f"{proxy_host_port(swapped['old']) or '—'} → "
                                 f"{proxy_host_port(proxy)}, беру окно заново",
                                 account=label, tag=job["tag"])
                        log(f"{label}: F5 не помог — меняю прокси "
                            f"{proxy_host_port(swapped['old']) or '—'} → "
                            f"{proxy_host_port(proxy)}, беру окно заново")
                        queue.put(job, front=True)
                        continue
                    if not empty["proxy_note"]:
                        empty["proxy_note"] = True  # один раз на аккаунт, не по окну
                        log(f"{label}: прокси сменить нечем — {swapped.get('reason')}; "
                            "лестница дальше как есть (добавить: панель → «Прокси»)")
                if root_win and not quiet:
                    if not job.get("empty_f5_done") and empty["f5_left"] and \
                            not args.no_browser:
                        # Сессия, которую VK считает старой, — частая причина тихого
                        # нуля: заход в ленту из профиля (F5) её освежает.
                        # --no-browser выключает. F5 — один раз на аккаунт за прогон:
                        # это самое дорогое действие (открывается браузер).
                        job["empty_f5_done"] = True
                        empty["f5_left"] -= 1
                        if _f5_refresh(client, session_path, proxy, label, show=show):
                            log(f"{label}: беру окно заново — сессия освежена через F5")
                            queue.put(job, front=True)
                            continue
                    if not job.get("empty_count") and count_req > EMPTY_RETRY_COUNT:
                        # Пауза не помогает, а другой count — это другой запрос: VK
                        # считает его заново. Меньшая страница листается дальше, посты
                        # не теряются.
                        job["empty_count"] = max(EMPTY_RETRY_COUNT, count_req // 4)
                        log(f"{label}: ВК молча отдаёт пустую выдачу — беру то же окно "
                            f"другим запросом: count {count_req} → {job['empty_count']}, "
                            "без паузы")
                        queue.put(job, front=True)
                        continue
                    if not job.get("empty_split") and empty["splits_left"] and \
                            int(job["end_time"]) - int(job["start_time"]) >= EMPTY_SPLIT_MIN_SEC:
                        if split_job(db, job, queue, int(settings["min_window_minutes"]) * 60):
                            job["empty_split"] = True
                            empty["splits_left"] -= 1
                            bump(split=1)
                            log(f"{label}: пусто и на другом запросе — делю окно пополам, "
                                "половинки беру отдельными запросами")
                            continue
                if not quiet and not job.get("empty_handover") and \
                        strike_win not in empty["handed"]:
                    # Запрос меняли, а выдача та же: не крутим окно на этом аккаунте —
                    # отдаём его другому аккаунту (по разу на окно).
                    job["empty_handover"] = True
                    empty["handed"].add(strike_win)
                    log(f"{label}: окно {win} молча пустое даже после смены запроса — "
                        "отдаю другому аккаунту")
                    queue.put(job)
                    continue
                if not quiet:
                    if root_win:
                        log(f"{label}: окно {win} молча пустое — оставляю следующему «Старту», "
                            "беру другие окна")
                    else:
                        log(f"{label}: половинка окна {win} молча пустая — оставляю "
                            "следующему «Старту»")
                if strike_win in empty["stuck"]:
                    continue
                empty["stuck"].add(strike_win)
                if not quiet:
                    # След в панели и в `state_db.py --events N`: пустые окна раньше не
                    # оставляли ни одной записи, и «прогон прошёл, а постов нет» было
                    # нечем объяснить. В общем «тихом» режиме по каждому окну не пишем —
                    # иначе панель и лог тонут в сотнях одинаковых строк.
                    db.event("warn", "slot",
                             f"{label}: {win} — ВК молча отдаёт пустую выдачу, "
                             "окно добьёт следующий «Старт»", account=label, tag=job["tag"])
                empty["strikes"] += 1
                if not quiet and empty["strikes"] >= EMPTY_STRIKES_HOLD and not empty["held"]:
                    empty["held"] = True
                    empty["quiet"] = True
                    log(f"{label}: ВК молча отдаёт пустую выдачу на {empty['strikes']} разных "
                        "окнах подряд — аккаунт в отлёжку, очередь добирают другие")
                    db.event("warn", "slot",
                             f"{label}: лестницу (F5 и дробление) не повторяю: окна беру "
                             "одним запросом, остальное добьёт следующий «Старт»",
                             account=label, tag=job["tag"])
                    log(f"{label}: дальше лестницу не повторяю — окна беру одним запросом")
                    return ("hold", "ВК отдаёт пустую выдачу")
                continue
            if res["status"] == "ok" and res["seen"] == 0 and not args.dry_run and \
                    not job.get("empty_checked") and db.tag_max_date(job["tag"]):
                # Канарейка в порядке (сессия жива), а окно пустое: возможно, VK режет
                # именно этот запрос, и отпустит он его через несколько минут. Повтор
                # ставим в КОНЕЦ очереди, а не перед собой: мгновенный повтор получает
                # тот же пустой ответ (в логе так и было — «пусто и на другом запросе»
                # подряд), а через круг очереди тихое ограничение обычно уже отпускает
                # (в прогоне 18.10 оно держалось ~7 минут). Так часы, по которым в базе
                # ещё ничего нет, не закрываются как «собрано» из-за минутной тишины.
                job["empty_checked"] = True
                db.update_job(job["id"], status="pending", reason="empty_recheck")
                queue.put(job)
                continue
            if res["seen"]:
                # Пришёл хоть один пост — серия пустых выдач кончилась: лестницу и
                # счётчики снова можно использовать. «stuck» и «handed» не трогаем:
                # одно окно — одно предупреждение и одна передача за прогон. Общий
                # счётчик «тихого нуля» тоже снимаем: VK снова отдаёт выдачу.
                empty["strikes"] = 0
                empty["f5_left"] = EMPTY_F5_RETRIES
                empty["proxy_left"] = EMPTY_PROXY_SWAPS_PER_RUN
                empty["splits_left"] = EMPTY_SPLITS_PER_RUN
                empty["quiet"] = False
                empty["held"] = False
                silent_reset()
                token_refreshes = 0
            if handle_result(db, job, res, queue, settings, label) == "stop":
                return ("hold", str(res.get("reason") or "аккаунт выбыл"))
            if not args.dry_run:
                db.add_account_items(label, res["new"])
                db.mark_account(label, proxy=proxy, status=READY, error="", touch_ok=True)
            pool.add_posts(acc, res["new"])
            maybe_live_export(db, settings, args)
        except Exception as exc:  # noqa: BLE001 — поток не должен умирать молча
            log(f"{label}: сбой на окне #{job['tag']}: {type(exc).__name__}: {exc}")
            # Сбой тоже считаем попыткой: без этого окно с устойчивой ошибкой
            # (например база занята) крутилось в очереди бесконечно.
            attempts = int(job.get("attempts") or 0) + 1
            job["attempts"] = attempts
            dropped = attempts >= int(settings["job_attempts"])
            try:
                db.update_job(
                    job["id"],
                    status="failed" if dropped else "pending",
                    reason=f"crash: {exc}",
                    attempts=attempts,
                )
            except Exception as inner:  # noqa: BLE001 — база молчит: окно не теряем
                log(f"{label}: состояние окна не записано ({inner})")
            if dropped:
                log(
                    f"{label}: окно #{job['tag']} #{job['id']} брошено после {attempts} сбоев"
                    " (вернётся в очередь при следующем «Старте»)"
                )
            else:
                queue.put(job)
            bump(errors=1)
            time.sleep(1)
        finally:
            queue.done()


_last_export = {"t": 0.0}


def maybe_live_export(db: DB, settings: dict, args) -> None:
    """Живой Excel: пишет только один поток за раз, остальные не ждут."""
    if args.no_export or args.dry_run:
        return
    if time.time() - _last_export["t"] < max(5, int(settings["export_interval_sec"])):
        return
    if not _export_lock.acquire(blocking=False):
        return
    try:
        _last_export["t"] = time.time()
        export_posts(db, EXPORTS_DIR / "vk_feed_live.xlsx", tag=args.tag_filter or None)
    except Exception as exc:  # noqa: BLE001
        log(f"живой экспорт не удался: {exc}")
    finally:
        _export_lock.release()


def merge_intervals(items: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Соседние и пересекающиеся отрезки сливаются: длина объединения без нахлёстов."""
    merged: list[list[int]] = []
    for lo, hi in sorted(items):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(a, b) for a, b in merged]


def clip_intervals(
    items: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """Отрезки внутри [start, end): всё, что вылезает за период, отрезается."""
    return [(a, b) for a, b in ((max(lo, start), min(hi, end)) for lo, hi in items) if b > a]


def subtract_intervals(start: int, end: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) минус уже собранные отрезки → дыры; дыры короче минуты отбрасываем."""
    gaps, cur = [], start
    for lo, hi in covered:
        if hi <= cur or lo >= end:
            continue
        if lo > cur:
            gaps.append((cur, lo))
        cur = max(cur, hi)
    if cur < end:
        gaps.append((cur, end))
    return [(a, b) for a, b in gaps if b - a >= 60]


class RoundPlanner:
    """Окна раунда порциями: пока период не пройден, окна не кончаются.

    Порция (воркеров × windows_per_worker) — это размер очереди, а не «работа на
    один запуск»: как только воркеры разобрали порцию, планировщик выдаёт
    следующую. Так «за 30 суток» с десятками тегов собирается одним запуском.
    Теги в порции идут по кругу, поэтому двигаются все теги сразу, а не первый
    по списку. Первый раунд — весь период [start_time, end) минус уже собранное,
    следующие — только свежие посты (от последнего собранного до «сейчас»).
    """

    def __init__(
        self,
        db: DB,
        tags: list[str],
        args,
        settings: dict,
        round_no: int,
        resume: Optional[list[dict]] = None,
    ) -> None:
        self.db, self.round_no = db, int(round_no)
        self.start = int(args.start_time)
        # Окна из прошлого запуска уже стоят в очереди — второй раз их не планируем.
        self._resume = {
            (str(j["tag"]), int(j["start_time"]), int(j["end_time"])) for j in (resume or [])
        }
        # +1 c к «сейчас»: пост, опубликованный в секунду запуска, не выпадет
        self.end = int(args.end_time) if args.end_time else int(time.time()) + 1
        workers = max(1, int(args.workers or settings["max_accounts"]))
        self.batch_size = workers * max(1, int(settings.get("windows_per_worker", 3)))
        self._queues: dict[str, list[tuple[int, int]]] = {}
        self._order: deque[str] = deque()
        self.total_windows = 0
        self.planned = 0
        # «Период» в отчётах — запрошенный отрезок [start, end), а не сумма длин
        # окон: перепроверяемые окна нахлёстываются друг на друга и вылезают за
        # границу периода, поэтому объём работы ≠ период.
        self.period_seconds = max(0, self.end - self.start)
        self.total_seconds = self.period_seconds
        self._tag_count = max(1, len(tags))
        # Охват периода: что уже собрано в базе + что роздано в этом прогоне.
        self._covered: dict[str, list[tuple[int, int]]] = {}
        self._covered_seconds = 0
        if self.round_no == 1:
            self._plan_period(tags, args, settings)
        else:
            self._plan_fresh(tags, args, settings)
        for tag in tags:  # порядок тегов — как в файле, дальше по кругу
            if self._queues.get(tag):
                self._order.append(tag)

    def _plan_period(self, tags: list[str], args, settings: dict) -> None:
        """Весь период минус уже собранные отрезки, окна — свежие первыми.

        Собранное не переделываем, но свежий хвост периода (refresh_tail_hours)
        перепроверяем: лента меняется — приходят запоздавшие посты, обновляются
        лайки и комментарии. Перепроверяем ровно те окна, что лежат в базе
        (теми же границами: ни новых строк, ни дробления), а непройденный
        остаток хвоста планируем как обычно. `--full` берёт весь период,
        `--no-refresh` — только то, что правда не собрано.
        """
        start = self.start
        size = int(float(settings["window_hours"]) * 3600)
        max_seen = int(settings["split_if_seen"])
        tail = 0 if args.full else int(float(settings.get("refresh_tail_hours") or 0) * 3600)
        tail_cut = self.end - tail if tail > 0 else self.end
        for tag in tags:
            covered = self.db.covered_intervals(tag, max_seen)
            self._seed_covered(tag, covered)  # охват периода: база + план
            if args.full:
                covered = []  # --full проходит период заново, собранное не вычитаем
            refresh = [] if (args.full or tail <= 0) else [
                (lo, hi) for lo, hi in self.db.covered_windows(tag, max_seen, tail_cut)
                if lo < self.end and hi > start
            ]
            if refresh:
                log(f"#{tag}: хвост {tail / 3600:.1f} ч перепроверяю — лента меняется "
                    f"(окон {len(refresh)}; --no-refresh — без повторов)")
            # Итог: старое собранное не трогаем, хвост — перепроверяем, остальное — собираем.
            older = [(lo, min(hi, tail_cut)) for lo, hi in covered if lo < tail_cut]
            gaps = subtract_intervals(start, self.end, older)
            skipped = (self.end - start) - sum(b - a for a, b in gaps)
            if skipped > 60:
                log(f"#{tag}: уже собрано {skipped / 3600:.1f} ч из периода — пропускаю "
                    f"(--full — пройти всё, --no-refresh — не перепроверять хвост)")
            windows = [
                w for g_lo, g_hi in gaps for w in self._gap_windows(g_lo, g_hi, refresh, size)
            ] + refresh
            windows = self._fresh_windows(tag, windows)
            windows.sort(key=lambda w: (-w[1], -w[0]))  # свежие первыми
            self._queues[tag] = windows
            self.total_windows += len(windows)

    @staticmethod
    def _gap_windows(
        lo: int, hi: int, refresh: list[tuple[int, int]], size: int
    ) -> list[tuple[int, int]]:
        """Дыра периода минус перепроверяемые окна, порезанная на стартовые окна.

        Перепроверяемые окна из дыры вычитаются: иначе одна и та же работа
        попала бы в очередь дважды — и своим окном, и новым поверх него.
        """
        keep = subtract_intervals(lo, hi, sorted(refresh)) if refresh else [(lo, hi)]
        return [w for a, b in keep for w in split_windows(a, b, size)]

    def _plan_fresh(self, tags: list[str], args, settings: dict) -> None:
        """Только свежие посты: от последнего собранного (с нахлёстом) до «сейчас»."""
        overlap = int(settings["repeat_overlap_sec"])
        max_seen = int(settings["split_if_seen"])
        for tag in tags:
            since = max(int(args.start_time), self.db.tag_max_date(tag) - overlap)
            windows = self._fresh_windows(tag, [(since, self.end)] if since < self.end else [])
            self._queues[tag] = windows
            self.total_windows += len(windows)
            # Собранное прошлыми раундами лежит в базе — в охват идёт и оно.
            self._seed_covered(tag, self.db.covered_intervals(tag, max_seen))

    def _fresh_windows(self, tag: str, windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Окна без тех, что уже стоят в очереди с прошлого запуска."""
        if not self._resume:
            return windows
        return [w for w in windows if (tag, w[0], w[1]) not in self._resume]

    def batch(self) -> list[dict]:
        """Следующая порция окон; [] — период пройден целиком."""
        jobs: list[dict] = []
        while self._order and len(jobs) < self.batch_size:
            tag = self._order.popleft()
            lo, hi = self._queues[tag].pop(0)  # окна тега уже отсортированы: свежие первыми
            jobs.append(self.db.add_job(tag, lo, hi, self.round_no))
            self.planned += 1
            self._mark_covered(tag, lo, hi)
            if self._queues[tag]:
                self._order.append(tag)  # у тега ещё есть окна — снова в конец круга
        return jobs

    def left_windows(self) -> int:
        """Сколько окон ещё не роздано в очередь (период пройден не полностью)."""
        return sum(len(w) for w in self._queues.values())

    def _seed_covered(self, tag: str, intervals: list[tuple[int, int]]) -> None:
        """Что уже собрано в базе: в охват идёт только часть внутри периода."""
        seeded = merge_intervals(clip_intervals(intervals, self.start, self.end))
        self._covered_seconds += sum(b - a for a, b in seeded)
        self._covered[tag] = seeded

    def _mark_covered(self, tag: str, lo: int, hi: int) -> None:
        """Окно роздано в работу — его часть внутри периода считаем охваченной."""
        before = sum(b - a for a, b in self._covered.get(tag, []))
        merged = merge_intervals(
            self._covered.get(tag, []) + clip_intervals([(lo, hi)], self.start, self.end)
        )
        self._covered[tag] = merged
        self._covered_seconds += sum(b - a for a, b in merged) - before

    def coverage_share(self) -> float:
        """Доля охваченного периода (0..1): собранное в базе + розданное в прогоне.

        Считается до раздачи окон (`batch`) как «что уже лежало в базе»: если период
        был собран ещё до «Старт», прогон сводится к перепроверке свежего хвоста.
        """
        if not self.total_seconds:
            return 1.0
        return (self._covered_seconds / self._tag_count) / self.total_seconds

    def progress_text(self) -> str:
        """Сколько периода охвачено (база + розданное): окна, часы и проценты.

        Знаменатель — длина запрошенного периода, а не сумма длин окон: окна
        нахлёстываются друг на друга и вылезают за границу периода, поэтому от
        объёма работы проценты уезжали за 100%. Числитель — часы периода, уже
        собранные в базе или розданные в этом прогоне (по объединению и в
        среднем по тегам).
        """
        covered = self._covered_seconds / self._tag_count
        total = self.total_seconds
        share = self.coverage_share() * 100
        return (
            f"окон {self.planned}/{self.total_windows}, "
            f"период {covered / 3600:.1f} ч из {total / 3600:.1f} ч ({share:.0f}%)"
        )


def run_round(jobs: list[dict], pool: AccountPool, workers: int, args, settings: dict, db: DB) -> int:
    """Гоняет окна воркерами (аккаунт берётся из пула); возвращает остаток очереди."""
    queue = JobQueue()
    for job in jobs:
        queue.put(job)
    threads = [
        threading.Thread(target=run_worker, args=(pool, queue, args, settings, db), name=f"acc{i + 1}")
        for i in range(max(1, workers))
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("остановка по Ctrl+C — дожидаюсь текущих запросов (прогресс сохранён)")
        _stop_flag.set()
        for t in threads:
            t.join(timeout=30)
    return queue.size()


def sleep_interruptible(seconds: int) -> bool:
    """False — прервали Ctrl+C."""
    try:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(1.0, end - time.time()))
        return True
    except KeyboardInterrupt:
        _stop_flag.set()
        return False


def finish_tags(db: DB, tags: list[str], args) -> None:
    if args.dry_run:
        return
    for tag in tags:
        left = db.count_open_jobs(tag)
        db.finish_tag(tag, status="partial" if left else "finished",
                      reason=f"окон не добито: {left}" if left else "all_windows_done")


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Прогон по хештегам VK (окна времени, многопоточно)")
    p.add_argument("listing", nargs="?", default="", help="файл со списком тегов")
    p.add_argument("--pages", type=int, default=0, help="макс. страниц на одно окно")
    p.add_argument("--count", type=int, default=0, help="постов на страницу (10..200)")
    p.add_argument("--passes", type=int, default=0, help="раундов всего (1 = без повторов)")
    p.add_argument("--watch", action="store_true", help="повторять раунды без конца")
    p.add_argument("--repeat-pause", type=int, default=0,
                   help="как часто проверять новые посты, с (по умолчанию из настроек)")
    p.add_argument("--run-hours", type=float, default=0,
                   help="работать столько часов от «Старт» (в панели «Работать, ч»): "
                        "период — это ЧТО собирать, а это — СКОЛЬКО работать; процесс "
                        "непрерывно добирает новые посты, пока время не выйдет "
                        "(0 — один проход, как раньше)")
    p.add_argument("--hours", type=float, default=0)
    p.add_argument("--days", type=float, default=0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--window-hours", type=float, default=0, help="размер стартового окна")
    p.add_argument("--workers", type=int, default=0, help="сколько аккаунтов задействовать")
    p.add_argument("--validate", action="store_true", help="проверить ВСЕ аккаунты и выйти")
    p.add_argument("--validate-first", action="store_true", help="проверить аккаунты, затем запустить")
    p.add_argument("--validate-workers", type=int, default=4, help="сколько аккаунтов проверять разом")
    p.add_argument("--no-validate", action="store_true", help="не проверять аккаунты, даже если готовых нет")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер при обновлении токена")
    p.add_argument("--no-proxy-swap", action="store_true",
                   help="не менять аккаунту прокси, когда ВК молча отдаёт пустую выдачу "
                        "даже после F5 (по умолчанию: взять свободный прокси из запаса, "
                        "прежний адрес вернуть в запас — data/proxy_stock.txt)")
    p.add_argument("--show-browser", action="store_true",
                   help="показывать браузер: окно обхода ленты (какое окно времени берётся) "
                        "и окно эмулятора, когда он освежает сессию через ленту (F5) "
                        "(то же — \"show_browser\": true в data/settings.json; "
                        "выключить: python src/settings.py --show-browser off)")
    p.add_argument("--tag-query", choices=("hash", "word", "auto"),
                   help="чем искать тег: hash — с решёткой (#тег, как было всегда), "
                        "word — словом, без решётки (посты, где тег встречается в "
                        "тексте), auto — выбрать по каждому тегу (где выдача больше). "
                        "То же — \"tag_query\" в data/settings.json "
                        "(переключатель «с #» / «без #» / «авто» на странице «Теги»)")
    p.add_argument("--proxy", default="", help="прокси для всех слотов")
    p.add_argument("--limit", type=int, default=0, help="взять первые N тегов")
    p.add_argument("--no-resume", action="store_true", help="бросить прерванные окна, начать заново")
    p.add_argument("--full", action="store_true", help="пройти весь период, даже уже собранные часы")
    p.add_argument("--refresh-hours", type=float, default=0,
                   help="сколько свежих часов периода перепроверять (0 — из настроек)")
    p.add_argument("--no-refresh", action="store_true",
                   help="не перепроверять собранный хвост периода: только новые часы")
    p.add_argument("--session-refresh-minutes", type=float, default=0,
                   help="как часто менять access_token аккаунтов во время прогона, мин "
                        "(0 — из data/settings.json)")
    p.add_argument("--no-session-refresh", action="store_true",
                   help="не обновлять сессии во время прогона")
    p.add_argument("--dry-run", action="store_true", help="не писать в базу")
    p.add_argument("--no-export", action="store_true", help="без живого Excel-экспорта")
    p.add_argument("--tag-filter", default="", help="для экспорта: только один тег")
    # совместимость со старыми командами — теперь не нужны
    p.add_argument("--retry", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--reset", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def resolve_period(args, settings: dict) -> None:
    now = int(time.time())
    args.start_time = probe_mod.parse_time(args.since)
    args.end_time = probe_mod.parse_time(args.until)
    if args.hours or args.days:
        args.start_time = int(now - (args.hours * 3600 + args.days * 86400))
    if not args.start_time:
        # без start_time VK отдаёт только последние 24 часа — задаём явно
        args.start_time = int(now - float(settings["default_days"]) * 86400)
    if args.end_time and args.start_time >= args.end_time:
        raise SystemExit("--since должен быть меньше --until")


def resume_windows(db: DB, leftover: list[dict], args, settings: dict) -> list[dict]:
    """Из окон прошлого запуска оставляем только те, что попадают в период.

    Без этой проверки запуск «за 6 часов» лез в прошлые недели: окна давнего
    прогона лежат в базе как pending и добирались в любом следующем запуске.
    Устаревшие окна снимаем с очереди (status = 'dropped'), иначе они всплывали
    бы каждый раз.

    Окна, все часы которых уже собраны другими окнами (status = 'done'), тоже
    снимаем: лента отдаёт по ним то же, что лежит в базе, — в логе это «постов
    N, новых 0», а запросы и время уходят впустую, пока непройденные часы ждут
    очереди. Запоздавшие посты добирает свежий хвост периода, весь период заново
    — --full. Окно, у которого несобранной осталась хоть минута, оставляем.
    """
    if not leftover:
        return []
    lo = int(args.start_time)
    hi = int(args.end_time) if args.end_time else int(time.time()) + 1
    max_seen = int(settings["split_if_seen"])
    keep: list[dict] = []
    stale: list[dict] = []
    covered_jobs: list[dict] = []
    covered: dict[str, list[tuple[int, int]]] = {}
    for job in leftover:
        start, end = int(job["start_time"]), int(job["end_time"])
        if end <= lo or start >= hi:
            stale.append(job)
            continue
        tag = str(job["tag"])
        if tag not in covered:
            covered[tag] = db.covered_intervals(tag, max_seen)
        if not subtract_intervals(start, end, covered[tag]):
            covered_jobs.append(job)
            continue
        keep.append(job)
    if stale:
        dropped = db.drop_jobs([int(j["id"]) for j in stale])
        oldest = min(int(j["start_time"]) for j in stale)
        log(
            f"окна прошлого запуска вне периода {fmt_ts(lo)} – {fmt_ts(hi)}: "
            f"брошено {dropped} (самое старое {fmt_ts(oldest)}; "
            f"--no-resume — бросить все)"
        )
    if covered_jobs:
        dropped = db.drop_jobs([int(j["id"]) for j in covered_jobs], reason="already_covered")
        hours = sum(int(j["end_time"]) - int(j["start_time"]) for j in covered_jobs) / 3600
        log(
            f"окна прошлого запуска уже собраны другими окнами: брошено {dropped} "
            f"({hours:.1f} ч — по ним лента отдаёт то же, «новых 0»; "
            f"--full — пройти период заново)"
        )
    return keep


def _warn_proxy_rules(accounts: list, db: DB) -> None:
    """Предупреждения о прокси. Запуск не блокируется.

    Правило:
      - выгруженный аккаунт (есть посты в базе) может делить адрес максимум с одним
        другим аккаунтом;
      - новый аккаунт (постов нет) адрес не делит ни с кем.
    """
    from accounts import proxy_sharing, resolve_proxy

    no_proxy = [a.login for a in accounts if not resolve_proxy(a)]
    if no_proxy:
        log(f"ВНИМАНИЕ: без своего прокси не в работе: {len(no_proxy)} — {', '.join(no_proxy)}")
    shared = proxy_sharing(accounts, {})
    for proxy, logins in shared.items():
        loaded = {l for l in logins if int((db.account_state(l) or {}).get("total_items") or 0) > 0}
        host = proxy.split("@")[-1]
        names = ", ".join(logins)
        if len(logins) > 2:
            log(f"ВНИМАНИЕ: прокси {host} обслуживает {len(logins)} аккаунтов ({names}) — больше двух не допускается")
        elif len(loaded) < len(logins):
            fresh = [l for l in logins if l not in loaded]
            log(f"ВНИМАНИЕ: новые аккаунты ({', '.join(fresh)}) делят прокси {host} ({names}) — новым общий адрес не допускается")
        else:
            log(f"предупреждение: выгруженные аккаунты делят прокси {host} ({names}) — допустимо, но не добавляйте третий")



def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    if args.window_hours:
        settings["window_hours"] = args.window_hours
    # Свежий хвост периода перепроверяем в каждом запуске: лента меняется,
    # поэтому уже собранные часы рядом с «сейчас» не считаем закрытыми.
    if args.no_refresh:
        settings["refresh_tail_hours"] = 0.0
    elif args.refresh_hours:
        settings["refresh_tail_hours"] = max(0.0, float(args.refresh_hours))
    args.pages = args.pages or int(settings["max_pages_per_tag"])
    args.count = min(200, max(10, args.count or int(settings["page_count"])))
    # Эмулятор браузера на виду: --show-browser или "show_browser": true в
    # data/settings.json. Флаг нормализуем здесь, чтобы все пути прогона (воркеры,
    # фон обновления сессии, F5 при пустой выдаче) читали одно значение из args.
    args.show_browser, why_hidden = resolve_show_browser(args, settings)
    if why_hidden:
        log(why_hidden)
    # Чем искать тег в запросе к VK: --tag-query или "tag_query" в data/settings.json
    # (переключатель «с #» / «без #» на странице «Теги»). Режим один на весь прогон,
    # поэтому ставим его в клиенте до первых запросов — воркеры берут общий режим,
    # а окно обхода показывает ту же выдачу. В args кладём готовое значение.
    args.tag_query = resolve_tag_query(args, settings)
    set_tag_query(args.tag_query)
    log(
        f"тег в запросе: {TAG_QUERY_LABELS[args.tag_query]}"
        + (
            " — ищу слово в тексте постов, решётку не подставляю "
            "(это другая выдача, чем #тег)"
            if args.tag_query == "word"
            else " — для каждого тега выберу сам: хештег или слово, где выдача больше"
            if args.tag_query == "auto"
            else ""
        )
    )
    # Период — это ЧТО собирать, а не сколько работать: один проход кончается, как
    # только период охвачен («за 6 часов» на коротком периоде — минута работы).
    # «Сколько работать» — --run-hours: процесс непрерывно добирает новые посты,
    # пока не выйдет время, а явный --passes остаётся пределом и для него, и для --watch.
    duty_hours = max(0.0, float(args.run_hours or 0))
    endless = args.watch or duty_hours > 0
    passes = args.passes or (10**9 if endless else int(settings["repeat_passes"]))
    repeat_pause = args.repeat_pause or int(settings["repeat_pause_sec"])
    resolve_period(args, settings)
    # Лимит скорости на аккаунт (settings.json). Это главный тормоз прогона:
    # VK режет всплески кодами 6/9, поэтому «быстрее» — это меньший зазор между
    # запросами одного аккаунта, а не больше воркеров на тех же аккаунтах.
    gap_lo, gap_hi = set_call_gap(settings["min_call_gap_sec"], settings["max_call_gap_sec"])

    listing = Path(args.listing) if args.listing else TAGS_FILE
    tags = parse_tags_file(listing)[: args.limit or None]
    if not tags:
        log(f"нет тегов в {listing} — добавьте по одному на строку")
        return 1

    db = DB(dedupe=settings["dedupe_posts"], min_text_chars=settings["min_text_chars"])
    # Авто-режим тега: режимы, выбранные прошлыми прогонами, уже лежат в базе —
    # поднимаем их, чтобы не тратить пробные запросы на каждый тег заново.
    seed_tag_modes(db)
    accounts = load_accounts(ACCOUNTS_FILE)

    # Проверка аккаунтов (независимо от числа рабочих потоков): --validate — только
    # проверить и выйти, --validate-first — проверить и сразу работать.
    if args.validate or args.validate_first:
        log(f"проверяю аккаунты ({len(accounts) or 1} шт., разом до {args.validate_workers})…")
        summary = validate_accounts(
            db,
            accounts,
            workers=args.validate_workers,
            tag=tags[0] if tags else "",
            proxy_override=args.proxy,
            use_browser=not args.no_browser,
            on_result=lambda r: log(f"  {r['login']}: {r['status']} — {r['reason']}"),
        )
        log(
            f"проверка: всего {summary['total']}, готовы {summary['ready']}, "
            f"отлёжка {summary['hold']}, мертвы {summary['dead']}"
        )
        if summary["ready"] == 0 and args.validate_first and not args.dry_run:
            log("ПРЕДПОЛЁТ: готовых аккаунтов нет — прогон не запущен")
            return 2
        if args.validate and not args.validate_first:
            return 0 if summary["dead"] == 0 else 2

    pool, workers = build_pool(
        accounts, db, args.workers or int(settings["max_accounts"]), args.proxy
    )
    _warn_proxy_rules(accounts, db)
    # Готовых нет (например, у всех протух токен) — один раз проверяем все: проверка
    # умеет обновлять токен из cookies или профиля браузера, так аккаунты оживают.
    # Если проверка уже была выше (--validate / --validate-first), не гоняем её снова.
    checked = bool(args.validate or args.validate_first)
    if not checked and not args.dry_run and not args.no_validate and not pool_has_ready(pool):
        log("нет готовых аккаунтов — проверяю все (токен обновляется автоматически)…")
        summary = validate_accounts(
            db,
            accounts,
            workers=args.validate_workers,
            tag=tags[0] if tags else "",
            proxy_override=args.proxy,
            use_browser=not args.no_browser,
            on_result=lambda r: log(f"  {r['login']}: {r['status']} — {r['reason']}"),
        )
        log(
            f"проверка: готовы {summary['ready']}, отлёжка {summary['hold']}, "
            f"мертвы {summary['dead']}"
        )
        pool, workers = build_pool(
            accounts, db, args.workers or int(settings["max_accounts"]), args.proxy
        )
    if workers == 0:
        log("нет живых аккаунтов — проверьте вход (python src/account_pool.py --validate)")
        return 2

    # Токен VK живёт ограниченное время, а прогон «за 30 суток» идёт часами: фон
    # меняет токен аккаунтов на свежий, чтобы посреди работы не выбывали живые.
    refresh_min = session_refresh_minutes(args, settings)
    if refresh_min > 0 and not args.dry_run:
        SessionRefresher(
            pool, {"session_refresh_minutes": refresh_min},
            no_browser=args.no_browser, show_browser=args.show_browser,
        ).start()
        log(
            f"сессию обновляю фоном каждые {refresh_min:g} мин "
            f"(session_refresh_minutes; --no-session-refresh — выключить)"
        )
    if args.show_browser and not args.no_browser:
        # Сказать прямо: окна будут открываться на виду — это ожидаемое поведение,
        # а не сбой. Показ идёт по двум линиям: окно обхода показывает, какое окно
        # времени берётся прямо сейчас, а эмулятор мелькает, когда сессию надо
        # освежить через ленту (F5) или обновить токен.
        log(
            "браузер работаю на виду (--show-browser): окно обхода покажет, какое "
            "окно времени берётся, а эмулятор откроется, когда сессию надо освежить "
            "через ленту (F5), — оба закроются сами с концом прогона"
        )

    # Недобитые окна прошлого запуска (только те, что внутри периода) + всегда
    # окна текущего периода: иначе при наличии старых окон свежие часы не
    # собирались бы, а «за 6 часов» лезло бы в прошлые недели.
    leftover = [] if args.no_resume else db.open_jobs(tags)
    if args.no_resume:
        log(f"брошено прерванных окон: {db.drop_open_jobs(tags)}")
    else:
        # Окна, брошенные после ошибок (разом протух токен и т.п.), возвращаем в
        # очередь: иначе эти часы больше никогда не перезапрашиваются — failed не
        # попадает ни в open_jobs, ни в «собрано».
        revived = 0
        if not args.dry_run:
            revived = db.revive_failed_jobs(
                tags, since=int(args.start_time or 0), until=int(args.end_time or 0)
            )
        if revived:
            log(f"возвращено в очередь брошенных окон (были ошибки): {revived}")
            leftover += db.open_jobs(tags)
        # Остатки вне запрошенного периода не добираем: «за 6 часов» — только 6 часов.
        # И те, чьи часы уже собраны другими окнами: по ним лента отдаёт то же.
        leftover = resume_windows(db, leftover, args, settings)
    planner = RoundPlanner(db, tags, args, settings, 1, resume=leftover)
    # Что уже лежало в базе ДО раздачи окон: если период был собран ещё до «Старт»,
    # прогон сведётся к перепроверке свежего хвоста и кончится за минуты — в конце
    # подскажем, как заставить его работать дольше («Работать, ч» / --run-hours).
    covered_before = planner.coverage_share()
    jobs = planner.batch()
    if leftover:
        newest = max(int(j["end_time"]) for j in leftover)
        log(
            f"+ недобитых окон с прошлого запуска: {len(leftover)} "
            f"(свежайшее {fmt_ts(newest)}; --no-resume — бросить их)"
        )
        jobs += leftover
    log(
        f"тегов {len(tags)}, воркеров {workers}, {describe_pool(pool)}, "
        f"период {fmt_ts(args.start_time)} – {fmt_ts(args.end_time or time.time())}, "
        f"по {args.count} постов/запрос, лимит {gap_lo:g}–{gap_hi:g} с/запрос"
    )
    if settings["dedupe_posts"] != "off":
        junk_note = (
            f", текст короче {settings['min_text_chars']} знаков — мусор"
            if settings["min_text_chars"] else ""
        )
        log(f"повторы по тексту не пишу (dedupe_posts={settings['dedupe_posts']}{junk_note})")
    if jobs:
        log(f"в очереди {len(jobs)} окон — {planner.progress_text()}")
    else:
        log("собирать нечего: период уже пройден целиком"
            " (--refresh-hours 12 — перепроверить свежие часы, --full — пройти всё заново)")

    left = 0
    started = time.time()
    duty_over = False
    silent_brk = False  # раунд прерван общим «тихим нулём»: дальше проходы не начинаем
    # Сколько новых постов было к началу последнего круга «Работать, ч»: круг, который
    # не добавил ни одного, — это «новых постов нет», и следующий ждёт «Пауза, с».
    round_new_mark = int(_stats["new"])
    silent_reset()  # «тихий ноль» считаем с нуля: прогон может идти в том же процессе
    if duty_hours:
        log(
            f"работаю {duty_hours:g} ч (до {fmt_ts(started + duty_hours * 3600)}): "
            f"непрерывно собираю новые посты («Стоп» — прервать)"
        )
    for round_no in range(1, passes + 1):
        if silent_brk:
            break  # VK режет выдачу всем: новые проходы бессмысленны
        if round_no > 1:
            if duty_hours and duty_left(started, duty_hours) <= 0:
                duty_over = True
                log(f"время вышло — завершаю ({planner.progress_text()})")
                break  # время вышло, пока шёл прошлый проход
            # Свежий хвост периода: добираем только новые посты.
            planner = RoundPlanner(db, tags, args, settings, round_no)
            jobs = planner.batch()
            if not jobs and duty_hours:
                # Новых постов нет: токен фоном сам обновляется (F5 при пустой ленте),
                # ждём недолго и проверяем снова.
                log(
                    f"новых постов нет — проверю снова "
                    f"(осталось {fmt_left(duty_left(started, duty_hours))})"
                )
                if not sleep_interruptible(repeat_pause):
                    break
                continue
        round_new_mark = int(_stats["new"])  # метка «что этот круг успел собрать»
        try:
            # Порция — это размер очереди, а не «работа на один запуск»: пока период
            # не пройден, воркеры получают следующую. Прогон идёт до конца периода.
            while True:
                if duty_hours and duty_left(started, duty_hours) <= 0:
                    # Время вышло посреди прохода: обрываем его, недобитые окна
                    # остаются в базе и их возьмёт следующий «Старт».
                    duty_over = True
                    log(f"время вышло — завершаю ({planner.progress_text()})")
                    break
                left = run_round(jobs, pool, workers, args, settings, db)
                if _stop_flag.is_set():
                    break
                if left and silent_stopped():
                    # VK молча режет выдачу всем аккаунтам: молотить очередь
                    # бесполезно — лестница и передача окон только множат запросы, а
                    # постов всё равно нет. Недобитые окна остались pending: их добьёт
                    # следующий заход, когда ограничение отпустит.
                    pending = (
                        resume_windows(db, db.open_jobs(tags), args, settings)
                        if not args.no_resume else []
                    )
                    if not pending:
                        # Прерывать нечего: VK ответил пусто, но незакрытых окон нет.
                        silent_reset()
                        left = 0
                        break
                    if duty_hours:
                        wait = max(int(repeat_pause), EMPTY_GLOBAL_BACKOFF_SEC)
                        silent_rearm()  # ещё пусто — снова упрёмся в порог и подождём
                        log(
                            f"ВК молча режет выдачу всем аккаунтам — пауза {fmt_left(wait)}, "
                            f"потом проверю снова (недобитых окон {len(pending)}; "
                            f"{planner.progress_text()})"
                        )
                        if not sleep_interruptible(wait):
                            break
                        jobs = pending
                        continue
                    log(
                        f"ВК молча режет выдачу всем аккаунтам — раунд прерван, "
                        f"недобитых окон {len(pending)} добьёт следующий «Старт» "
                        f"({planner.progress_text()})"
                    )
                    silent_reset()
                    left = 0  # не «аккаунты кончились»: причина уже сказана в логе
                    silent_brk = True
                    break
                if left:
                    break
                jobs = planner.batch()
                if not jobs:
                    break
                log(f"в очереди ещё {len(jobs)} окон — {planner.progress_text()}")
        except KeyboardInterrupt:
            _stop_flag.set()
            log("остановка по Ctrl+C — прогресс сохранён (окна и курсоры в базе)")
        if _stop_flag.is_set():
            log(
                f"остановлено на {planner.progress_text()}; "
                f"окон ждёт в плане: {planner.left_windows()} — «Старт» продолжит с этого места"
            )
            break
        if left:
            log(
                f"живых аккаунтов не осталось — {planner.progress_text()}; "
                f"запуск позже добьёт остаток (окон в очереди {left})"
            )
            break
        if duty_hours and not duty_over and duty_left(started, duty_hours) <= 0:
            duty_over = True
            log(f"время вышло — завершаю ({planner.progress_text()})")
            break
        if duty_hours and not duty_over and duty_round_waits(duty_hours, round_new_mark):
            # Круг не принёс ни одного нового поста: VK отдаёт по свежему хвосту то
            # же, что уже в базе (или молчит про самые свежие часы). Повторять круг
            # сразу нечем — окно то же, ответ тот же, а каждое повторение это лишний
            # запрос, лестница (F5, другой count, дробление) и новая строка окна в
            # базе. Раньше прогон крутил пустой хвост каждые пару секунд (в логе «в
            # очереди ещё 6 окон» раз в секунды, к следующему «Старту» — десятки
            # «недобитых» окон). Ждём «Пауза, с» и проверяем снова, как и написано
            # в README.
            log(
                f"новых постов нет — проверю снова "
                f"(осталось {fmt_left(duty_left(started, duty_hours))})"
            )
            if not sleep_interruptible(repeat_pause):
                break

    finish_tags(db, tags, args)
    stop_feed_viewer()  # окно обхода живёт весь прогон — закрываем его здесь
    stats = snapshot()
    elapsed = max(1.0, time.time() - started)
    print("─" * 60)
    log(
        f"итого за {elapsed / 60:.1f} мин: окон {stats['jobs']} (делений {stats['split']}), "
        f"страниц {stats['pages']}, новых постов {stats['new']} "
        f"({stats['new'] / elapsed * 60:.0f}/мин), ошибок {stats['errors']} "
        f"(капча {stats['captcha']}, токен {stats['token']}, пустых выдач {stats['throttle']})"
    )
    if stats["dupes"] or stats["junk"]:
        log(
            f"не записано: повторов по тексту {stats['dupes']}, "
            f"мусорных постов {stats['junk']}"
        )
    if not endless and planner.total_seconds > 0 and covered_before >= 0.999:
        # Период был собран ещё до «Старт»: работы на все окна не было, только
        # перепроверка свежего хвоста — потому прогон и кончился за минуты. Раньше
        # это выглядело как «период 6 часов, а работа на минуту» без объяснений.
        span = f"{planner.total_seconds / 3600:g}"
        log(
            f"период уже был собран целиком до запуска — работа сводилась к перепроверке "
            f"свежего хвоста ({planner.progress_text()}). Занять процесс на время: "
            f"«Работать, ч» {span} (--run-hours {span})"
        )
    if stats["token"] and workers < len(accounts):
        log("токены части аккаунтов просрочены — повторный вход: python src/vk_auth.py --login")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    if not args.no_export and not args.dry_run:
        try:
            path = EXPORTS_DIR / f"vk_feed_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            export_posts(db, path, tag=args.tag_filter or None)
        except Exception as exc:  # noqa: BLE001
            log(f"финальный экспорт не удался: {exc}")
    # Работа по времени кончилась сама (окно времени вышло) — это нормальный конец, а не ошибка,
    # даже если в плане остались недобитые окна: их возьмёт следующий «Старт».
    return 0 if (left == 0 or duty_over) and not _stop_flag.is_set() else 2


if __name__ == "__main__":
    raise SystemExit(main())
