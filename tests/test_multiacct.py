#!/usr/bin/env python3
"""Проверка работы с парком аккаунтов: пул, ротация, статусы, прокси.

    python tests/test_multiacct.py

Сеть не нужна: VK подменяется заглушками, база и файлы — во временной папке.
Проверяем, что окна не теряются, когда аккаунт выбывает посреди прогона, что
статусы (работает / отлёжка / мёртв) доходят до базы и до окна, а прокси
раздаются аккаунтам по строкам файла.
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
import vk_auth  # noqa: E402
from account_pool import DEAD, HOLD, READY  # noqa: E402
from accounts import load_accounts, resolve_proxy  # noqa: E402
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
accounts_file.write_text(
    "# телефон;пароль;2FA;прокси\n"
    + "\n".join(f"{phone};пароль;;" for phone in PHONES)
    + "\n",
    encoding="utf-8",
)
accounts = load_accounts(accounts_file)
check(len(accounts) == 3, f"в файле три аккаунта: {[a.login for a in accounts]}")

sessions = sorted(acc.session_path.name for acc in accounts)
check(len(set(sessions)) == 3, f"у каждого аккаунта своя сессия: {sessions}")
check(all(name.startswith("session_") for name in sessions), "имя сессии — session_<телефон>.json")

proxy_pool = ["1.1.1.1:1111:u:p", "2.2.2.2:2222:u:p", "3.3.3.3:3333:u:p"]
proxies = [resolve_proxy(acc, proxy_pool) for acc in accounts]
check(len(set(proxies)) == 3, f"каждому аккаунту свой прокси: {proxies}")
check(all(p.startswith("http://u:p@") for p in proxies), "прокси нормализованы в url")

check(
    [resolve_proxy(acc, ["direct", "none", "-"]) for acc in accounts] == ["", "", ""],
    "direct/none/- в пуле прокси — работа без прокси",
)
check(resolve_proxy(accounts[0], []) == "", "пустой пул прокси — тоже без прокси")

direct_file = tmp / "direct.txt"
direct_file.write_text("+79990000009;пароль;;direct\n", encoding="utf-8")
check(
    resolve_proxy(load_accounts(direct_file)[0], proxy_pool) == "",
    "direct в строке аккаунта перекрывает пул прокси",
)

# 2. пул берёт все аккаунты, воркеров — сколько просили
db = DB(tmp / "parser.sqlite3")
settings = load_settings()
pool, workers = run_tags.build_pool(accounts, db, 2, "")
check(len(pool) == 3, "в пуле все аккаунты, а не только рабочие")
check(workers == 2, f"воркеров столько, сколько заказано: {workers}")

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

rows = {row["login"]: row for row in db.list_accounts()}
check(rows[PHONES[0]]["status"] == DEAD, f"протухший токен → мёртв: {rows[PHONES[0]]['status']}")
check(
    rows[PHONES[1]]["status"] == HOLD and rows[PHONES[1]]["cooldown_until"] > 0,
    f"капча → отлёжка: {rows[PHONES[1]]['status']}",
)
check(rows[PHONES[2]]["status"] == READY, f"рабочий аккаунт остался готов: {rows[PHONES[2]]['status']}")
check(
    rows[PHONES[2]]["total_items"] == len(jobs) * 4,
    f"рабочему аккаунту записали посты всех окон: {rows[PHONES[2]]['total_items']}",
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

# 6. пустой прокси у аккаунта — это «напрямую», а не прокси из файла сессии
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

shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "МУЛЬТИАККАУНТ РАБОТАЕТ")
raise SystemExit(1 if FAILED else 0)
