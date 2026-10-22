#!/usr/bin/env python3
"""Проверка смены прокси: пустая выдача после F5 — пробуем другой адрес.

    python tests/test_proxy_swap.py

Сеть и браузер не нужны: VK подменяется заглушками, accounts.txt, запас прокси и
база — во временной папке (настоящие data/ и конфиги тест не трогает).

Проверяем то, из-за чего окна стояли молча пустыми, хотя аккаунт живой:

  1) proxy_stock.swap_account_proxy: аккаунт получает ПЕРВЫЙ свободный прокси из
     запаса, прежний адрес возвращается в запас в конец, занятый адрес другого
     аккаунта не раздаётся, пустой запас и незнакомый аккаунт дают понятную причину;
  2) лестница в run_tags.work_account: после F5, который не помог, аккаунту меняется
     прокси — и в accounts.txt, и в живом клиенте, и в базе, — окно берётся заново
     уже с нового адреса, а в панель уходит событие (иначе непонятно, почему поехал
     адрес аккаунта);
  3) смена одна на аккаунт за прогон, --no-proxy-swap выключает шаг целиком, а при
     пустом запасе прогон просто пишет причину и работает дальше;
  4) vk_client.VkFeedClient.set_proxy переключает и уже поднятую HTTP-сессию (иначе
     запросы пошли бы по старому адресу и проба ничего бы не проверила).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import proxy_stock  # noqa: E402
import run_tags  # noqa: E402
import vk_auth  # noqa: E402
from accounts import load_accounts  # noqa: E402
from settings import load_settings  # noqa: E402
from state_db import DB  # noqa: E402
from vk_client import VkFeedClient  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def write_session(session_path: Path, token: str) -> dict:
    """Пишет файл сессии так, как это делает vk_auth."""
    payload = {
        "access_token": token,
        "token_expires_at": int(time.time()) + 86400,
        "api_version": "5.289",
        "login": Path(session_path).stem.replace("session_", ""),
    }
    Path(session_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


tmp = Path(tempfile.mkdtemp(prefix="proxy_swap_"))
accounts_file = tmp / "accounts.txt"
stock_file = tmp / "proxy_stock.txt"
OLD = "http://u:p@1.1.1.1:8000"
OTHER = "http://u:p@2.2.2.2:8000"
FREE_1 = "http://u:p@9.9.9.9:8000"
FREE_2 = "http://u:p@8.8.8.8:8000"

accounts_file.write_text(
    "# телефон;пароль;2FA;прокси\n"
    f"79990000001;pass;secret;{OLD}\n"
    f"79990000002;pass;secret;{OTHER}\n",
    encoding="utf-8",
)
stock_file.write_text(
    "# свободные прокси\n" + "\n".join((OTHER, FREE_1, FREE_2)) + "\n",
    encoding="utf-8",
)


def swap(login: str, current: str = "") -> dict:
    """Смена прокси на временных файлах — так же, как её зовёт прогон."""
    return proxy_stock.swap_account_proxy(
        login, current, accounts_path=accounts_file, stock_path=stock_file
    )


def account_proxy(login: str) -> str:
    for acc in load_accounts(accounts_file):
        if acc.login == login:
            return acc.proxy_raw
    return ""


# 1. смена адреса: свободный из запаса, прежний — назад в запас
res = swap("79990000001", OLD)
check(
    res["ok"] and res["new"] == FREE_1 and res["old"] == OLD,
    f"аккаунт получил первый свободный прокси из запаса: {res}",
)
check(account_proxy("79990000001") == FREE_1, "в accounts.txt записан новый адрес")
check(
    account_proxy("79990000002") == OTHER,
    f"прокси другого аккаунта не тронут: {account_proxy('79990000002')}",
)
stock = proxy_stock.read_stock(stock_file)
check(
    stock == [FREE_2, OLD],
    f"прежний адрес вернулся в запас в конец, занятый убран: {stock}",
)
check(
    stock_file.read_text(encoding="utf-8").startswith("# свободные прокси"),
    "в запасе остался заголовок — файл читается человеком",
)

# 2. следующий аккаунт получает следующий свободный, а не адрес соседа
res2 = swap("79990000002", OTHER)
check(
    res2["ok"] and res2["new"] == FREE_2,
    f"второй аккаунт получил второй свободный прокси (не занятый): {res2}",
)
stock_now = proxy_stock.read_stock(stock_file)
check(
    FREE_1 not in stock_now and OLD in stock_now,
    f"занятые адреса из запаса не раздаются: {stock_now}",
)

# 3. пустой запас и незнакомый аккаунт — понятная причина, без исключений
proxy_stock.write_stock([], stock_file)
empty = swap("79990000001", FREE_1)
check(
    not empty["ok"] and "нет свободных" in empty["reason"],
    f"пустой запас — понятная причина: {empty}",
)
stranger = swap("79990009999", "")
check(
    not stranger["ok"] and "accounts.txt" in stranger["reason"],
    f"аккаунта нет в файле — смены нет: {stranger}",
)
check(
    account_proxy("79990000001") == FREE_1,
    "пустой запас и чужой логин не меняют accounts.txt",
)

# 4. лестница прогона: F5 не помог — меняем адрес и берём окно заново
settings = load_settings()
db = DB(tmp / "parser.sqlite3")
logs: list[str] = []
windows: list[tuple[int, int]] = []
silent: list[tuple[int, int]] = []


def fake_window(_client, _db, _tag, **kw):
    """VK молчит на «пустые» окна и отдаёт посты на остальные."""
    lo, hi = int(kw.get("start_time") or 0), int(kw.get("end_time") or 0)
    windows.append((lo, hi))
    if any(s_lo <= lo and hi <= s_hi for s_lo, s_hi in silent):
        return {"status": "ok", "reason": "end_of_feed", "seen": 0, "new": 0, "pages": 1,
                "need_split": False, "cursor": ""}
    return {"status": "ok", "reason": "end_of_feed", "seen": 7, "new": 7, "pages": 1,
            "need_split": False, "cursor": ""}


class FakeClient:
    """Клиент аккаунта: воркеру нужны токен и смена прокси на ходу."""

    def __init__(self, login: str, proxy: str = "") -> None:
        self.login = login
        self.token = "vk1.a.живой"
        self.api_version = "5.289"
        self.proxy = proxy
        self.proxy_history: list[str] = [proxy]

    def set_proxy(self, proxy: str) -> str:
        self.proxy = proxy
        self.proxy_history.append(proxy)
        return proxy


class FakePool:
    """Пул-заглушка: воркеру нужен только add_posts."""

    def __init__(self) -> None:
        self.posts = 0

    def add_posts(self, _acc, count) -> None:
        self.posts += int(count or 0)


LOGIN = "79990000001"
acc = type("Acc", (), {
    "login": LOGIN,
    "proxy": FREE_1,
    "session_path": tmp / "session_79990000001.json",
    "cooldown_until": 0.0,
})()
write_session(acc.session_path, "vk1.a.живой")

orig_window = run_tags.probe_mod.run_window
orig_throttle = run_tags.empty_is_throttle
orig_export = run_tags.maybe_live_export
orig_reload = vk_auth.token_via_feed_reload
orig_swap = run_tags.swap_account_proxy
run_tags.log = logs.append
run_tags.probe_mod.run_window = fake_window
run_tags.empty_is_throttle = lambda *_a, **_kw: True  # канарейка: посты есть, а выдача пуста
run_tags.maybe_live_export = lambda *_a, **_kw: None
run_tags.swap_account_proxy = lambda login, current="": swap(login, current)
f5 = {"calls": 0}


def fake_reload(session_path, proxy_raw="", timeout=90, headless=True):
    """F5 в профиле: свежая сессия, но выдача у VK та же (тихий ноль)."""
    f5["calls"] += 1
    return write_session(session_path, f"vk1.a.после-F5-{f5['calls']}")


vk_auth.token_via_feed_reload = fake_reload

# запас снова полон, а в accounts.txt — только проверяемый аккаунт: смена должна
# взять ровно первый свободный адрес (порядок в запасе и проверяем)
proxy_stock.write_stock([FREE_2, "http://u:p@7.7.7.7:8000"], stock_file)
accounts_file.write_text(
    "# телефон;пароль;2FA;прокси\n" f"79990000001;pass;secret;{FREE_1}\n",
    encoding="utf-8",
)


def run_args(*extra: str):
    """Аргументы прогона, как их получает work_account из main."""
    args = run_tags.parse_args(list(extra))
    args.dry_run = False
    args.no_export = True
    args.tag_filter = ""
    args.start_time = 0
    args.end_time = 0
    return args


try:
    args = run_args()
    run_tags.silent_reset()
    SILENT = (1_700_000_000, 1_700_000_000 + 2 * 3600)
    silent.append(SILENT)
    queue = run_tags.JobQueue()
    queue.put(db.add_job("маникюр", *SILENT, 1))
    client = FakeClient(LOGIN, acc.proxy)
    before = len(windows)
    start = len(logs)
    outcome, reason = run_tags.work_account(
        client, acc, queue, args, settings, db, FakePool()
    )
    fresh = logs[start:]
    check(
        any("меняю прокси" in line for line in fresh),
        f"после F5 адрес меняется: {[line for line in fresh if 'прокси' in line][:3]}",
    )
    check(
        acc.proxy == FREE_2 and client.proxy == FREE_2,
        f"новый адрес доехал и до аккаунта, и до клиента: "
        f"{acc.proxy} / {client.proxy} / {client.proxy_history}",
    )
    check(
        account_proxy(LOGIN) == FREE_2,
        f"accounts.txt обновлён — следующий прогон возьмёт новый адрес: "
        f"{account_proxy(LOGIN)}",
    )
    check(
        run_tags.proxy_host_port(FREE_1) in " ".join(fresh)
        and run_tags.proxy_host_port(FREE_2) in " ".join(fresh),
        "в логе видно, с какого адреса на какой перешли",
    )
    check(
        "u:p@" not in " ".join(fresh),
        f"в логе нет логина и пароля прокси: {[line for line in fresh if 'прокси' in line][:2]}",
    )
    check(
        len(windows) - before > 1,
        f"окно взято заново уже с нового адреса: запросов {len(windows) - before}",
    )
    check(
        proxy_stock.read_stock(stock_file) == ["http://u:p@7.7.7.7:8000", FREE_1],
        f"старый адрес вернулся в запас: {proxy_stock.read_stock(stock_file)}",
    )
    trail = [
        e for e in db.recent_events(50)
        if e["level"] == "warn" and "меняю прокси" in str(e["message"])
    ]
    check(
        len(trail) == 1 and trail[0]["account"] == LOGIN,
        f"в панели видно, почему у аккаунта сменился адрес: {[e['message'] for e in trail]}",
    )
    check(
        f5["calls"] == 1,
        f"F5 остаётся первым шагом (адрес меняем уже после него): вызовов {f5['calls']}",
    )

    # 5. на аккаунт за прогон — одна смена: дальше лестница идёт как есть
    proxy_after = account_proxy(LOGIN)
    stock_after = proxy_stock.read_stock(stock_file)
    swap_lines = len([line for line in logs if "меняю прокси" in line])
    queue2 = run_tags.JobQueue()
    job2 = db.add_job("маникюр", SILENT[0] + 5 * 3600, SILENT[0] + 7 * 3600, 1)
    queue2.put(job2)
    silent.append((job2["start_time"], job2["end_time"]))
    start = len(logs)
    run_tags.work_account(
        FakeClient(LOGIN, acc.proxy), acc, queue2, args, settings, db, FakePool()
    )
    check(
        len([line for line in logs if "меняю прокси" in line]) == swap_lines,
        f"второе пустое окно адрес не меняет (смена — одна на прогон): {logs[start:]}",
    )
    check(
        account_proxy(LOGIN) == proxy_after
        and proxy_stock.read_stock(stock_file) == stock_after,
        "accounts.txt и запас после второго окна те же",
    )

    # 6. --no-proxy-swap выключает шаг целиком
    proxy_stock.write_stock([FREE_2], stock_file)
    accounts_file.write_text(
        "# телефон;пароль;2FA;прокси\n" f"79990000005;pass;secret;{OLD}\n",
        encoding="utf-8",
    )
    quiet_login = "79990000005"
    quiet_acc = type("Acc", (), {
        "login": quiet_login,
        "proxy": OLD,
        "session_path": tmp / "session_79990000005.json",
        "cooldown_until": 0.0,
    })()
    write_session(quiet_acc.session_path, "vk1.a.живой")
    no_swap_args = run_args("--no-proxy-swap")
    run_tags.silent_reset()
    SILENT_2 = (1_700_100_000, 1_700_100_000 + 3600)
    silent.append(SILENT_2)
    queue3 = run_tags.JobQueue()
    queue3.put(db.add_job("ногти", *SILENT_2, 1))
    start = len(logs)
    run_tags.work_account(
        FakeClient(quiet_login, OLD), quiet_acc, queue3, no_swap_args, settings, db, FakePool()
    )
    check(
        not [line for line in logs[start:] if "меняю прокси" in line],
        f"--no-proxy-swap: адрес не меняем: "
        f"{[line for line in logs[start:] if 'прокси' in line][:2]}",
    )
    check(
        account_proxy(quiet_login) == OLD and quiet_acc.proxy == OLD,
        "с --no-proxy-swap accounts.txt и аккаунт остались на прежнем адресе",
    )

    # 7. пустой запас: шаг есть, но смены нет — причина в логе, лестница идёт дальше
    empty_login = "79990000006"
    accounts_file.write_text(
        "# телефон;пароль;2FA;прокси\n" f"79990000006;pass;secret;{OLD}\n",
        encoding="utf-8",
    )
    empty_acc = type("Acc", (), {
        "login": empty_login,
        "proxy": OLD,
        "session_path": tmp / "session_79990000006.json",
        "cooldown_until": 0.0,
    })()
    write_session(empty_acc.session_path, "vk1.a.живой")
    run_tags.silent_reset()
    proxy_stock.write_stock([], stock_file)
    SILENT_3 = (1_700_200_000, 1_700_200_000 + 3600)
    silent.append(SILENT_3)
    queue4 = run_tags.JobQueue()
    queue4.put(db.add_job("гель", *SILENT_3, 1))
    start = len(logs)
    run_tags.work_account(
        FakeClient(empty_login, OLD), empty_acc, queue4, run_args(), settings, db, FakePool()
    )
    no_stock = [line for line in logs[start:] if "сменить нечем" in line]
    check(
        len(no_stock) == 1 and "нет свободных" in no_stock[0],
        f"пустой запас — одна понятная строка в логе, а не по окну: {no_stock}",
    )
    check(
        empty_acc.proxy == OLD and account_proxy(empty_login) == OLD,
        "без запаса адрес аккаунта остался прежним",
    )
finally:
    run_tags.probe_mod.run_window = orig_window
    run_tags.empty_is_throttle = orig_throttle
    run_tags.maybe_live_export = orig_export
    run_tags.swap_account_proxy = orig_swap
    vk_auth.token_via_feed_reload = orig_reload
    run_tags._browser_prefer_until.clear()
    run_tags.silent_reset()

# 8. set_proxy: живая HTTP-сессия тоже переключается на новый адрес
plain = VkFeedClient("vk1.a.тест", OLD)
check(plain.proxy == OLD, f"клиент помнит прокси из конструктора: {plain.proxy}")


class FakeSession:
    proxies = {"http": OLD, "https": OLD}


plain._session = FakeSession()
plain.set_proxy(FREE_1)
check(
    plain.proxy == FREE_1 and plain._session.proxies == {"http": FREE_1, "https": FREE_1},
    f"поднятая сессия переключилась на новый прокси: {plain._session.proxies}",
)
plain.set_proxy("")
check(
    plain._session.proxies == {},
    "пустой прокси снимает настройку сессии (аккаунт уходит напрямую)",
)

db.close()
shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "СМЕНА ПРОКСИ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)




