#!/usr/bin/env python3
"""Авто-режим тега: чем искать тег — хештегом (#тег) или словом без решётки.

    python tests/test_tag_mode_auto.py

Сеть, браузер и настоящие данные не нужны: клиент — заглушка, база — временный
SQLite. Проверяем то, из-за чего нишевый тег показывал «0 постов»:

  1) vk_client.detect_tag_mode: два пробных запроса (хештег и слово за сутки) и
     выбор режима, где постов больше; при равенстве — хештег;
  2) run_tags.tag_mode_for: «hash»/«word» из настроек не тратят запросов; в «авто»
     выбор запоминается в базе (tags.query_mode), второй раз проба не идёт; пусто
     в обоих режимах — не запоминаем; ошибка пробы — безопасный хештег;
  3) probe.run_window: выбранный режим доходит до запроса (mode=…), а не теряется
     в общем режиме прогона;
  4) миграция старой базы: колонка query_mode добавляется без потери данных.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import probe as probe_mod  # noqa: E402
import run_tags  # noqa: E402
import state_db  # noqa: E402
import vk_client  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def quiet(fn, *args, **kwargs):
    """Зовём то, что печатает в лог, — свой вывод теста не засоряем."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class FakeClient:
    """Заглушка VkFeedClient: отдаёт заданное число постов на каждый режим."""

    def __init__(self, hash_items: int = 0, word_items: int = 0) -> None:
        self.counts = {"hash": hash_items, "word": word_items}
        self.calls: list[dict] = []

    def search_hashtag(self, tag, *, count=50, start_from=None, start_time=None,
                       end_time=None, mode=None):
        self.calls.append(
            {
                "tag": tag,
                "count": count,
                "start_time": start_time,
                "end_time": end_time,
                "mode": mode,
            }
        )
        n = self.counts.get(mode, 0) if mode in self.counts else self.counts.get("hash", 0)
        items = [{"owner_id": -1, "post_id": i, "date": 0} for i in range(n)]
        return {"tag": tag, "items": items, "next_from": "", "total": n}


tmp = Path(tempfile.mkdtemp(prefix="tag_mode_"))
try:
    # 1. Проба: где выдача больше, там и ищем.
    client = FakeClient(hash_items=7, word_items=171)
    mode, hash_items, word_items = vk_client.detect_tag_mode(
        client, "#бровист", hours=24, now=1_800_000_000
    )
    check(
        (mode, hash_items, word_items) == ("word", 7, 171),
        f"хештегом 7, словом 171 — выбран word: {(mode, hash_items, word_items)}",
    )
    check(
        [call["mode"] for call in client.calls] == ["hash", "word"],
        f"проба спрашивает обе выдачи явно, не полагаясь на общий режим: "
        f"{[call['mode'] for call in client.calls]}",
    )
    check(
        client.calls[0]["start_time"] == 1_800_000_000 + 1 - 24 * 3600
        and client.calls[0]["end_time"] == 1_800_000_000 + 1,
        "проба смотрит последние сутки — свежая выдача, а не весь период",
    )
    check(client.calls[0]["tag"] == "бровист", "решётка из тега снимается")

    tied = FakeClient(hash_items=5, word_items=5)
    check(
        vk_client.detect_tag_mode(tied, "маникюр")[0] == "hash",
        "при равенстве остаётся хештег — прежнее поведение",
    )

    # 2. Прогон: режим на тег, выбор живёт в базе.
    db = state_db.DB(tmp / "parser.sqlite3")

    no_calls = FakeClient(hash_items=1, word_items=99)
    check(
        run_tags.tag_mode_for(db, no_calls, "маникюр", "hash") == "hash"
        and run_tags.tag_mode_for(db, no_calls, "маникюр", "word") == "word",
        "явный режим из настроек не тратит пробных запросов",
    )
    check(not no_calls.calls, "запросов к VK не было — режим взят из настройки")

    auto = FakeClient(hash_items=7, word_items=171)
    check(
        run_tags.tag_mode_for(db, auto, "#бровист", "auto") == "word",
        "в «авто» тег ищется словом: выдача больше",
    )
    check(
        db.tag_query_mode("бровист") == "word",
        "выбор записан в базу (tags.query_mode) — следующий прогон его вспомнит",
    )
    check(len(auto.calls) == 2, f"проба — ровно два запроса, не больше: {len(auto.calls)}")

    remembered = FakeClient(hash_items=100, word_items=1)
    check(
        run_tags.tag_mode_for(db, remembered, "бровист", "auto") == "word",
        "второй раз режим берётся из памяти прогона, а не из новой пробы",
    )
    check(not remembered.calls, "повторной пробы нет — режим уже выбран")

    # Новая память прогона: режим поднимается из базы, пробных запросов нет.
    run_tags.seed_tag_modes(db)
    after_seed = FakeClient(hash_items=0, word_items=50)
    check(
        run_tags.tag_mode_for(db, after_seed, "бровист", "auto") == "word"
        and not after_seed.calls,
        "следующий запуск берёт режим из базы — пробных запросов нет",
    )

    empty = FakeClient(hash_items=0, word_items=0)
    check(
        run_tags.tag_mode_for(db, empty, "новыйтег", "auto") == "hash",
        "пусто и хештегом, и словом — безопасный хештег",
    )
    check(
        db.tag_query_mode("новыйтег") == "",
        "пустой результат не запоминаем: следующий прогон проверит снова",
    )

    broken = FakeClient()

    def boom(*args, **kwargs):
        raise RuntimeError("сеть легла")

    broken.search_hashtag = boom  # type: ignore[method-assign]
    check(
        run_tags.tag_mode_for(db, broken, "сломанный", "auto") == "hash",
        "ошибка пробы не валит окно: ищем хештегом",
    )
    check(db.tag_query_mode("сломанный") == "", "ошибку тоже не запоминаем")

    # 3. Режим доходит до запроса окна (а не теряется в общем режиме прогона).
    collector = FakeClient(hash_items=0, word_items=2)
    res = quiet(
        probe_mod.run_window,
        collector,
        None,
        "бровист",
        start_time=1_800_000_000 - 3600,
        end_time=1_800_000_000,
        count=50,
        max_pages=2,
        dry_run=True,
        mode="word",
    )
    check(
        res["status"] == "ok" and res["seen"] == 2,
        f"окно собрало посты словом: {res['seen']}",
    )
    check(
        collector.calls and collector.calls[0]["mode"] == "word",
        f"режим окна ушёл в запрос: {collector.calls and collector.calls[0]['mode']}",
    )

    # 4. Старая база: колонки query_mode нет — миграция добавляет её на месте.
    old_path = tmp / "old.sqlite3"
    old = sqlite3.connect(old_path)
    old.executescript(
        "CREATE TABLE tags (query TEXT PRIMARY KEY,"
        " status TEXT NOT NULL DEFAULT 'pending', stop_reason TEXT, next_from TEXT,"
        " total_items INTEGER NOT NULL DEFAULT 0, total_pages INTEGER NOT NULL DEFAULT 0,"
        " passes_done INTEGER NOT NULL DEFAULT 0, first_seen_at REAL NOT NULL DEFAULT 0,"
        " last_run_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0);"
        "INSERT INTO tags(query, status) VALUES('маникюр', 'finished');"
    )
    old.commit()
    old.close()
    old_db = state_db.DB(old_path)
    columns = [row[1] for row in old_db.con.execute("PRAGMA table_info(tags)").fetchall()]
    check("query_mode" in columns, "в старой базе появилась колонка tags.query_mode")
    check(
        old_db.tag_state("маникюр").get("status") == "finished",
        "данные старой базы не потерялись",
    )
    old_db.set_tag_query_mode("маникюр", "hash")
    check(old_db.tag_query_mode("маникюр") == "hash", "режим пишется и читается")
    old_db.set_tag_query_mode("маникюр", "мусор")
    check(old_db.tag_query_mode("маникюр") == "hash", "непонятный режим не затирает выбор")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    vk_client.set_tag_query("hash")

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "АВТО-РЕЖИМ ТЕГА: ХЕШТЕГ ИЛИ СЛОВО — ПО ФАКТУ ВЫДАЧИ")
raise SystemExit(1 if FAILED else 0)
