#!/usr/bin/env python3
"""Проверка работы с парком аккаунтов: пул, ротация, статусы, прокси.

    python tests/test_multiacct.py

Сеть не нужна: VK подменяется заглушками, база и файлы — во временной папке.
Проверяем, что окна не теряются, когда аккаунт выбывает посреди прогона, что
статусы (работает / отлёжка / мёртв) доходят до базы и до окна, а прокси
берётся только у своего аккаунта (столбец в accounts.txt; аккаунт без прокси
в работу не идёт).
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

import gui  # noqa: E402
import run_tags  # noqa: E402
import state_db  # noqa: E402
import vk_auth  # noqa: E402
from account_pool import DEAD, HOLD, NEEDS_PROXY, READY, check_account  # noqa: E402
from accounts import (  # noqa: E402
    Account,
    load_accounts,
    normalize_proxy,
    proxy_sharing,
    resolve_proxy,
)
from settings import load_settings  # noqa: E402
from state_db import DB  # noqa: E402
from vk_client import VkFeedClient  # noqa: E402

PHONES = ("+79990000001", "+79990000002", "+79990000003")
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


# 1. аккаунты, их сессии и прокси
tmp = Path(tempfile.mkdtemp(prefix="multiacct_"))
accounts_file = tmp / "accounts.txt"
# Прокси — только свой, четвёртым полем строки: раздачи из пула больше нет.
account_proxies = ["1.1.1.1:1111:u:p", "2.2.2.2:2222:u:p", "3.3.3.3:3333:u:p"]
accounts_file.write_text(
    "# телефон;пароль;2FA;прокси\n"
    + "\n".join(f"{phone};пароль;;{proxy}" for phone, proxy in zip(PHONES, account_proxies))
    + "\n",
    encoding="utf-8",
)
accounts = load_accounts(accounts_file)
check(len(accounts) == 3, f"в файле три аккаунта: {[a.login for a in accounts]}")

sessions = sorted(acc.session_path.name for acc in accounts)
check(len(set(sessions)) == 3, f"у каждого аккаунта своя сессия: {sessions}")
check(all(name.startswith("session_") for name in sessions), "имя сессии — session_<телефон>.json")

proxies = [resolve_proxy(acc) for acc in accounts]
check(len(set(proxies)) == 3, f"каждому аккаунту свой прокси: {proxies}")
check(all(p.startswith("http://u:p@") for p in proxies), "прокси нормализованы в url")
check(
    [resolve_proxy(acc, ["9.9.9.9:1:u:p"]) for acc in accounts] == proxies,
    "прокси берётся из строки аккаунта, а переданный список-пул игнорируется",
)
check(
    normalize_proxy("9.9.9.9:1:u:p") == "http://u:p@9.9.9.9:1"
    and normalize_proxy("host:8080") == "http://host:8080",
    "прокси вида host:port:user:pass и host:port понимаются",
)

no_proxy_file = tmp / "no_proxy.txt"
no_proxy_file.write_text(
    "# телефон;пароль;2FA;прокси\n"
    + "\n".join(f"{phone};пароль;;" for phone in PHONES)
    + "\n",
    encoding="utf-8",
)
blank = load_accounts(no_proxy_file)
check(
    [resolve_proxy(acc, ["1.1.1.1:1111:u:p"]) for acc in blank] == ["", "", ""],
    "аккаунт без прокси в строке остаётся без прокси — из пула не раздаём",
)

direct_file = tmp / "direct.txt"
direct_file.write_text("+79990000009;пароль;;direct\n", encoding="utf-8")
check(
    resolve_proxy(load_accounts(direct_file)[0], ["1.1.1.1:1111:u:p"]) == "",
    "direct в строке аккаунта — работа без прокси",
)

shared_file = tmp / "shared.txt"
shared_file.write_text(
    "+79990000011;пароль;;1.1.1.1:1111:u:p\n"
    "+79990000012;пароль;;1.1.1.1:1111:u:p\n"
    "+79990000013;пароль;;2.2.2.2:2222:u:p\n",
    encoding="utf-8",
)
check(
    proxy_sharing(load_accounts(shared_file), {})
    == {"http://u:p@1.1.1.1:1111": ["+79990000011", "+79990000012"]},
    "два аккаунта на одном прокси видны как общий адрес",
)

# 2. пул берёт все аккаунты, воркеров — сколько просили
db = DB(tmp / "parser.sqlite3")
settings = load_settings()
pool, workers = run_tags.build_pool(accounts, db, 2, "")
check(len(pool) == 3, "в пуле все аккаунты, а не только рабочие")
check(workers == 2, f"воркеров столько, сколько заказано: {workers}")

blank_pool, blank_workers = run_tags.build_pool(blank, db, 2, "")
check(blank_workers == 0, f"аккаунты без прокси в работу не идут: воркеров {blank_workers}")
check(
    all(item["status"] == NEEDS_PROXY for item in blank_pool.snapshot()),
    f"у них статус «нужен прокси»: {[item['status'] for item in blank_pool.snapshot()]}",
)

# 3. прогон: первый аккаунт выбывает, второй уходит в отлёжку
calls = {"windows": 0, "logins": []}


def fake_make_client(session_path, proxy, pause, label):
    calls["logins"].append(label)
    return type("Client", (), {"login": label, "token": "t", "api_version": "5.199"})()


def fake_run_window(client, db, tag, **kw):
    calls["windows"] += 1
    if client.login == PHONES[0]:  # токен протух
        return {"status": "error", "reason": "token", "seen": 0, "new": 0, "pages": 0,
                "need_split": False, "cursor": ""}
    if client.login == PHONES[1]:  # капча
        return {"status": "error", "reason": "captcha", "seen": 0, "new": 0, "pages": 0,
                "need_split": False, "cursor": ""}
    return {"status": "ok", "reason": "done", "seen": 10, "new": 4, "pages": 1,
            "need_split": False, "cursor": ""}


def no_token(*_args, **_kwargs):
    raise RuntimeError("нужен повторный вход")


run_tags.make_client = fake_make_client
run_tags.probe_mod.run_window = fake_run_window
run_tags.maybe_live_export = lambda *a, **k: None
vk_auth.refresh_from_cookies = no_token
vk_auth.refresh_via_browser = no_token

tag = "маникюр"
jobs = [
    db.add_job(tag, 1_700_000_000 + i * 3600, 1_700_000_000 + (i + 1) * 3600, 1)
    for i in range(8)
]
args = run_tags.parse_args([])
args.dry_run = False
args.no_export = True
args.count = 100
args.pages = 10
args.tag_filter = ""

left = run_tags.run_round(jobs, pool, 2, args, settings, db)
done = db.con.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'done'").fetchone()["n"]
check(left == 0, f"очередь добита целиком (осталось окон: {left})")
check(int(done) == len(jobs), f"все окна закрыты: {done} из {len(jobs)}")
check(db.count_open_jobs(tag) == 0, "недобитых окон в базе нет")
check(calls["windows"] >= len(jobs) + 2, f"окна выбывших переиграли: запросов {calls['windows']}")
check(set(calls["logins"]) == set(PHONES), f"в работе побывали все: {sorted(set(calls['logins']))}")

rows = {state_db.login_key(row["login"]): row for row in db.list_accounts()}
check(rows[state_db.login_key(PHONES[0])]["status"] == DEAD,
      f"протухший токен → мёртв: {rows[state_db.login_key(PHONES[0])]['status']}")
check(
    rows[state_db.login_key(PHONES[1])]["status"] == HOLD
    and rows[state_db.login_key(PHONES[1])]["cooldown_until"] > 0,
    f"капча → отлёжка: {rows[state_db.login_key(PHONES[1])]['status']}",
)
check(rows[state_db.login_key(PHONES[2])]["status"] == READY,
      f"рабочий аккаунт остался готов: {rows[state_db.login_key(PHONES[2])]['status']}")
check(
    rows[state_db.login_key(PHONES[2])]["total_items"] == len(jobs) * 4,
    f"рабочему аккаунту записали посты всех окон: {rows[state_db.login_key(PHONES[2])]['total_items']}",
)

# 4. то же видят окно и следующий запуск
summary = gui.pool_summary(db.list_accounts())
check(summary == {READY: 1, HOLD: 1, DEAD: 1}, f"окно показывает работает/отлёжка/мёртв = {summary}")

pool2, workers2 = run_tags.build_pool(accounts, db, 3, "")
board = {item["login"]: item["status"] for item in pool2.snapshot()}
check(board[PHONES[0]] == DEAD and board[PHONES[1]] == HOLD, f"статусы из базы в пуле: {board}")
check(workers2 == 2, f"воркеров не больше живых аккаунтов: {workers2}")

db.mark_account(PHONES[1], status=HOLD, error="капча", cooldown_until=0.0)
pool3, _ = run_tags.build_pool(accounts, db, 3, "")
board3 = {item["login"]: item["status"] for item in pool3.snapshot()}
check(board3[PHONES[1]] == READY, f"истёкшая отлёжка снова готова: {board3[PHONES[1]]}")

# 5. в файле тегов комментарии не становятся тегами
tags_file = tmp / "tags.txt"
tags_file.write_text(
    "// заметка\n"
    "# заметка через решётку и пробел\n"
    "#маникюр\n"
    "педикюр\n"
    "маникюр\n",
    encoding="utf-8",
)
file_tags = run_tags.parse_tags_file(tags_file)
check(file_tags == ["маникюр", "педикюр"], f"в файле тегов только теги: {file_tags}")
check(
    gui.tags_in_file(tags_file) == {"маникюр", "педикюр"},
    "дубли и комментарии в файле не считаются тегами",
)

added, bad = gui.parse_tags_text("# заметка\n// и это\nманикюр, педикюр")
check(added == ["маникюр", "педикюр"], f"вставленные комментарии не создают теги: {added}")
check(not bad, f"комментарии не считаются ошибками: {bad}")

added, bad = gui.parse_tags_text("#комментарий\n")
check(added == ["комментарий"], f"решётка без пробела — это тег: {added}")

# 6. один аккаунт — одна строка, как бы ни был записан телефон
db6 = DB(tmp / "logins.sqlite3")
db6.mark_account("+79990000001", status=READY)
db6.add_account_items("79990000001", 5)
db6.add_account_items("+7 999 000-00-01", 2)
rows6 = db6.list_accounts()
check(
    len(rows6) == 1 and rows6[0]["login"] == "79990000001" and rows6[0]["total_items"] == 7,
    f"«+7…» и «7…» — один аккаунт: {[(r['login'], r['total_items']) for r in rows6]}",
)
check(
    db6.account_state("+79990000001").get("total_items") == 7,
    "состояние аккаунта ищется по любому написанию телефона",
)
db6.con.execute(
    "INSERT INTO accounts(login, status, total_items, fail_count, updated_at)"
    " VALUES('+79990000002', 'ready', 0, 2, 0)"
)
db6.add_account_items("79990000002", 42)
moved = state_db._migrate_account_logins(db6.con)
merged = {row["login"]: row for row in db6.list_accounts()}
check(moved == 1 and set(merged) == {"79990000001", "79990000002"}, f"двойник слит: {sorted(merged)}")
check(
    merged["79990000002"]["total_items"] == 42 and merged["79990000002"]["fail_count"] == 2,
    f"счётчики старой строки сохранены: {dict(merged['79990000002'])}",
)

# 7. пустой прокси у аккаунта — это «напрямую», а не прокси из файла сессии
session_file = tmp / "session_direct.json"
session_file.write_text(
    json.dumps(
        {
            "access_token": "токен",
            "token_expires_at": int(time.time()) + 3600,
            "proxy": "http://u:p@1.2.3.4:8080",
            "login": PHONES[0],
        }
    ),
    encoding="utf-8",
)
check(
    VkFeedClient.from_session(session_file, "", auto_refresh=False).proxy == "",
    "пустой прокси аккаунта — «напрямую»; файл сессии его не подменяет",
)
check(
    VkFeedClient.from_session(session_file, "direct", auto_refresh=False).proxy == "",
    "direct в прокси аккаунта — тоже без прокси",
)
check(
    VkFeedClient.from_session(session_file, auto_refresh=False).proxy
    == "http://u:p@1.2.3.4:8080",
    "без явного прокси берётся сохранённый в сессии",
)
check(
    VkFeedClient.from_session(session_file, "9.9.9.9:1:u:p", auto_refresh=False).proxy
    == "http://u:p@9.9.9.9:1",
    "явный прокси аккаунта важнее сохранённого в сессии",
)

# 8. Проверка аккаунта: VK ответил кодом 5 «access_token has expired» — токен
# обновляем (профиль браузера → cookies) и аккаунт остаётся рабочим. Случай из
# жизни: у обоих аккаунтов токен протух ровно так, проверка сразу говорила
# «мёртв — access_token has expired», «Старт» не запускался вовсе, хотя вход был
# не нужен: хватало обмена cookies. Обновление шло только по коду 1117.
expired = {"cookies": 0, "browser": 0}
vk_api_error = vk_auth.VkApiError
session8 = tmp / "session_79990000091.json"
session8.write_text('{"access_token": "vk1.a.Old"}', encoding="utf-8")
acc8 = Account(login="+79990000091", session_file=session8, proxy_raw="1.1.1.1:1111:u:p")
proxy8 = "http://u:p@1.1.1.1:1111"


def token_looks_alive(_data):
    """В файле токен ещё «не просрочен» — VK считает иначе (так и было)."""
    return True


def validate_expired(token, _proxy):
    if token == "vk1.a.Old":
        raise vk_api_error(5, "User authorization failed: access_token has expired.")
    return {"id": 42, "first_name": "Тест", "last_name": "Тестов"}


def browser_fails(*_args, **_kwargs):
    expired["browser"] += 1
    raise RuntimeError("браузер не поднялся")


def refresh_by_cookies(session_path, _proxy):
    expired["cookies"] += 1
    Path(session_path).write_text('{"access_token": "vk1.a.Fresh"}', encoding="utf-8")
    return {"access_token": "vk1.a.Fresh"}


vk_auth.token_alive = token_looks_alive
vk_auth.validate_token = validate_expired
vk_auth.refresh_via_browser = browser_fails
vk_auth.refresh_from_cookies = refresh_by_cookies

res8 = check_account(acc8, proxy8)
check(res8["status"] == READY, f"«access_token has expired» (код 5) оживает обменом cookies: {res8}")
check(res8["profile"] == "Тест Тестов", f"профиль читается свежим токеном: {res8.get('profile')}")
check(expired == {"cookies": 1, "browser": 1}, f"браузер пробовали первым, потом cookies: {expired}")

# Обмен прошёл, а VK и новый токен не принял — цикл не крутится, аккаунт мёртв.
def always_expired(*_args, **_kwargs):
    raise vk_api_error(5, "User authorization failed: access_token has expired.")


vk_auth.validate_token = always_expired
expired.update(cookies=0, browser=0)
session8.write_text('{"access_token": "vk1.a.Old"}', encoding="utf-8")
dead8 = check_account(acc8, proxy8)
check(
    dead8["status"] == DEAD and dead8["reason"].startswith("после обновления токена"),
    f"новый токен не приняли — «мёртв» без повторов: {dead8['reason']}",
)
check(expired == {"cookies": 1, "browser": 1}, f"одна попытка обновления, без рекурсии: {expired}")

# Обмен не удался вовсе (cookies тоже мертвы) — «мёртв» с понятной причиной.
def cookies_fail(*_args, **_kwargs):
    expired["cookies"] += 1
    raise RuntimeError("в сессии нет remixsid")


vk_auth.refresh_from_cookies = cookies_fail
expired.update(cookies=0, browser=0)
dead8b = check_account(acc8, proxy8)
check(dead8b["status"] == DEAD, f"сессия правда умерла — «мёртв»: {dead8b}")
check(
    "обновить не удалось" in dead8b["reason"] and "expired" in dead8b["reason"],
    f"в причине видно и что не вышло, и что ответил VK: {dead8b['reason']}",
)
check(expired == {"cookies": 1, "browser": 1}, f"и здесь по одной попытке: {expired}")

shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "МУЛЬТИАККАУНТ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)
