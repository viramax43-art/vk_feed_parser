#!/usr/bin/env python3
"""Проверка повторов и скорости: дедупликация по тексту, чистка базы, лимит запросов.

    python tests/test_dedupe.py

Сеть не нужна: базы — во временной папке, VK не вызывается. Проверяем:
- нормализацию текста (ссылки/хештеги/эмодзи не мешают сравнению повторов);
- dedupe_posts: off / tag / global — что именно не пишется в базу;
- min_text_chars: мусор вида «#ногти #маникюр» отсеивается, счётчики видны;
- выгрузку: один пост = одна строка, теги собраны через запятую;
- «200 постов / 262 ссылки» — пост один, привязок к тегам несколько;
- брошенные окна (failed) возвращаются в очередь только внутри периода;
- чистку старой базы: find_text_dupes + drop_posts;
- сброс состояния тега («В очередь») и счётчиков тега (reset_tag_stats, --reset-stats);
- лимит скорости: set_call_gap реально меняет паузу между запросами;
- чтение и валидацию новых ключей data/settings.json.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import settings as settings_mod  # noqa: E402
import sqlite3  # noqa: E402
import state_db  # noqa: E402
import vk_client  # noqa: E402
from settings import load_settings  # noqa: E402
from state_db import DB, norm_text  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


def post(key: str, text: str, date: int = 1_700_000_000, likes: int = 0) -> dict:
    """Минимальный пост в том виде, в каком его отдаёт vk_client.normalize_post."""
    owner, pid = key.split("_")
    return {
        "key": key,
        "owner_id": int(owner),
        "post_id": int(pid),
        "url": f"https://vk.com/wall{key}",
        "date": date,
        "date_str": "",
        "text": text,
        "author": {"name": "тест", "kind": "group", "url": ""},
        "likes": likes,
        "comments": 0,
        "reposts_count": 0,
        "views": 0,
        "attachments": [],
        "photos": 0,
        "videos": 0,
        "docs": 0,
        "audios": 0,
    }


tmp = Path(tempfile.mkdtemp(prefix="dedupe_"))

# 1. Нормализация текста для сравнения повторов
check(
    norm_text("Маникюр! https://vk.com/x #ногти") == "маникюр",
    f"ссылка и хештег вырезаны: {norm_text('Маникюр! https://vk.com/x #ногти')!r}",
)
check(norm_text("🔥 #ногти #маникюр") == "", "пост из одних хештегов даёт пустой текст")
check(
    norm_text("Ищу модель\n\n  Томск  ") == "ищу модель томск",
    f"регистр и пробелы схлопнуты: {norm_text('Ищу модель\n\n  Томск  ')!r}",
)

# 2. dedupe_posts = "off" — как раньше, пишем всё подряд
db_off = DB(tmp / "off.sqlite3", dedupe="off")
stats: dict = {}
db_off.add_posts([post("-1_1", "#ногти #маникюр"), post("-2_1", "#ногти #маникюр")],
                 "маникюр", stats=stats)
check(db_off.count_posts() == 2, "off: оба повтора записаны")
check(int(stats.get("dupes") or 0) == 0, "off: счётчик повторов пуст")

# 3. dedupe_posts = "tag" — повтор по тому же тегу не пишем, по другому тегу пишем
db_tag = DB(tmp / "tag.sqlite3", dedupe="tag")
stats = {}
db_tag.add_posts([post("-1_1", "Ищу модель на маникюр, Томск")], "маникюр", stats=stats)
db_tag.add_posts([post("-2_1", "ищу  модель на маникюр томск")], "маникюр", stats=stats)
check(db_tag.count_posts() == 1, "tag: повтор по тому же тегу не записан")
check(int(stats.get("dupes") or 0) == 1, f"tag: счётчик повторов = 1 ({stats.get('dupes')})")
db_tag.add_posts([post("-2_1", "ищу  модель на маникюр томск")], "педикюр")
check(db_tag.count_posts() == 2, "tag: в другом теге тот же текст пишется")

# 4. dedupe_posts = "global" — повтор не пишем ни по одному тегу
db_glob = DB(tmp / "global.sqlite3", dedupe="global")
db_glob.add_posts([post("-1_1", "Одно и то же объявление")], "маникюр")
db_glob.add_posts([post("-2_1", "одно   и то же объявление")], "педикюр")
check(db_glob.count_posts() == 1, "global: повтор не пишется ни по одному тегу")

# 5. min_text_chars — мусор из одних хештегов не пишем
db_junk = DB(tmp / "junk.sqlite3", dedupe="tag", min_text_chars=5)
stats = {}
db_junk.add_posts(
    [post("-1_1", "#ногти #маникюр"), post("-1_2", "Наращивание ресниц Томск")],
    "маникюр",
    stats=stats,
)
check(db_junk.count_posts() == 1, "мусор из хештегов не записан")
check(int(stats.get("junk") or 0) == 1, f"счётчик мусора = 1 ({stats.get('junk')})")

# 6. Выгрузка: один пост = одна строка, теги через запятую
db_multi = DB(tmp / "multi.sqlite3")
db_multi.add_posts([post("-1_1", "Ногти Томск, запись")], "маникюр")
db_multi.add_posts([post("-1_1", "Ногти Томск, запись")], "педикюр")
rows = db_multi.export_rows()
check(len(rows) == 1, f"выгрузка: строк {len(rows)} на один пост (раньше было бы 2)")
check(
    bool(rows) and set((rows[0]["tag"] or "").split(",")) == {"маникюр", "педикюр"},
    f"в строке все теги поста: {rows[0]['tag']!r}" if rows else "строк нет",
)
st = db_multi.stats()
check(
    st["posts"] == 1 and st["tag_links"] == 2,
    f"«200 постов и 262 ссылки» — это 1 пост и 2 привязки: {st['posts']} / {st['tag_links']}",
)

# 7. Брошенные окна возвращаются в очередь только внутри периода
db_jobs = DB(tmp / "jobs.sqlite3")
old = db_jobs.add_job("маникюр", 1000, 2000, 1)
db_jobs.update_job(old["id"], status="failed", attempts=3, reason="api_1117")
inside = db_jobs.add_job("маникюр", 5000, 6000, 1)
db_jobs.update_job(inside["id"], status="failed", attempts=3, reason="api_1117")
revived = db_jobs.revive_failed_jobs(["маникюр"], since=4000, until=7000)
check(revived == 1, f"оживлено окон: {revived} (только внутри периода)")
row = db_jobs.con.execute("SELECT * FROM jobs WHERE id = ?", (int(inside["id"]),)).fetchone()
check(row["status"] == "pending" and int(row["attempts"]) == 0, "окно внутри периода снова pending")
row = db_jobs.con.execute("SELECT * FROM jobs WHERE id = ?", (int(old["id"]),)).fetchone()
check(row["status"] == "failed", "окно вне периода осталось брошенным")

# 8. Чистка старой базы: find_text_dupes + drop_posts
db_clean = DB(tmp / "clean.sqlite3", dedupe="off")
db_clean.add_posts(
    [
        post("-1_1", "Одинаковый текст", date=1000, likes=1),
        post("-2_1", "Одинаковый текст", date=2000, likes=9),
        post("-3_1", "Уникальный текст", date=3000),
    ],
    "маникюр",
)
dupes = db_clean.find_text_dupes()
check(len(dupes) == 1 and dupes[0]["item_key"] == "-2_1",
      f"дубль найден: {[d['item_key'] for d in dupes]}")
kept_likes = db_clean.find_text_dupes(keep="likes")
check(len(kept_likes) == 1 and kept_likes[0]["item_key"] == "-1_1",
      "keep=likes оставляет самый популярный пост")
removed = db_clean.drop_posts([d["item_key"] for d in dupes])
check(removed == 1 and db_clean.count_posts() == 2,
      f"удалён повтор: осталось постов {db_clean.count_posts()}")
check(
    db_clean.con.execute("SELECT COUNT(*) n FROM tag_posts").fetchone()["n"] == 2,
    "привязки удалённого поста тоже убраны",
)

# 9. text_norm пишется при записи и добирается у старых постов
db_norm = DB(tmp / "norm.sqlite3")
db_norm.add_posts([post("-1_1", "Маникюр в Томске!")], "маникюр")
norm = db_norm.con.execute("SELECT text_norm FROM posts").fetchone()["text_norm"]
check(norm == "маникюр в томске", f"text_norm записан: {norm!r}")
db_norm.con.execute("UPDATE posts SET text_norm = ''")
filled = state_db._backfill_text_norm(db_norm.con)
again = db_norm.con.execute("SELECT text_norm FROM posts").fetchone()["text_norm"]
check(filled == 1 and again == "маникюр в томске", "старые посты добирают text_norm (апгрейд базы)")

# 10. Лимит скорости: set_call_gap реально меняет паузу между запросами
check(
    vk_client.set_call_gap(0.05, 0.06) == (0.05, 0.06) and vk_client.call_gap() == (0.05, 0.06),
    "лимит запросов меняется через set_call_gap",
)
vk_client._last_call_at.clear()
vk_client._throttle("тест-аккаунт")
started = time.time()
vk_client._throttle("тест-аккаунт")
waited = time.time() - started
check(0.04 <= waited <= 0.5, f"пауза учла новый лимит: {waited:.3f} с")
check(vk_client.set_call_gap(-5, -1) == (0.0, 0.0), "отрицательный лимит выключает паузы")
vk_client.set_call_gap(7, 9)

# 11. Кривые настройки подтягиваются к разумным
saved = settings_mod.CONFIG_FILE
settings_mod.CONFIG_FILE = tmp / "settings.json"
settings_mod.CONFIG_FILE.write_text(
    '{"dedupe_posts": "хрень", "min_call_gap_sec": -3, "max_call_gap_sec": -9,'
    ' "min_text_chars": -4}',
    encoding="utf-8",
)
bad = settings_mod.load_settings()
settings_mod.CONFIG_FILE = saved
check(
    bad["dedupe_posts"] == "tag"
    and bad["min_call_gap_sec"] == 0.0
    and bad["max_call_gap_sec"] == 0.0
    and bad["min_text_chars"] == 0,
    f"кривые настройки исправлены: {bad['dedupe_posts']}, "
    f"{bad['min_call_gap_sec']}–{bad['max_call_gap_sec']}, {bad['min_text_chars']}",
)
live = load_settings()
check(
    {"min_call_gap_sec", "max_call_gap_sec", "dedupe_posts", "min_text_chars"} <= set(live),
    "в data/settings.json читаются новые ключи",
)

# 12. Старая база (без text_norm) обновляется сама и не ломает панель
old_path = tmp / "old.sqlite3"
raw = sqlite3.connect(old_path)
raw.executescript(
    "CREATE TABLE posts (item_key TEXT PRIMARY KEY, owner_id INTEGER, post_id INTEGER,"
    " url TEXT, date INTEGER, date_str TEXT, text TEXT, author_name TEXT, author_kind TEXT,"
    " author_url TEXT, likes INTEGER, comments INTEGER, reposts INTEGER, views INTEGER,"
    " attachments TEXT, photos INTEGER, videos INTEGER, docs INTEGER, audios INTEGER,"
    " first_tag TEXT, seen_at REAL, payload TEXT);"
    "INSERT INTO posts(item_key, owner_id, post_id, text, date, first_tag, seen_at, payload)"
    " VALUES('-9_9', -9, 9, 'Старый пост', 1, 'маникюр', 0, '{}');"
)
raw.commit()
raw.close()
upgraded = DB(old_path, dedupe="global")
cols = [row[1] for row in upgraded.con.execute("PRAGMA table_info(posts)")]
norm_col = upgraded.con.execute("SELECT text_norm FROM posts").fetchone()["text_norm"]
check(
    "text_norm" in cols and norm_col == "старый пост",
    f"старая база добирает text_norm без потерь ({norm_col!r})",
)
indexes = [row[1] for row in upgraded.con.execute("PRAGMA index_list(posts)")]
check("idx_posts_text_norm" in indexes, "индекс по text_norm создан при обновлении базы")
upgraded.add_posts([post("-8_8", "Старый пост")], "маникюр", stats={})
check(
    upgraded.count_posts() == 1,
    f"после обновления базы дедупликация уже работает (постов {upgraded.count_posts()})",
)

# 13. Счётчики тега обнуляются отдельно от его состояния
db_stats = DB(tmp / "stats.sqlite3")
db_stats.bump_tag("маникюр", items=120, pages=4, next_from="курсор", pass_no=2)
db_stats.reset_tags(["маникюр"])
state = db_stats.tag_state("маникюр")
check(
    int(state["total_items"]) == 120 and state["status"] == "pending",
    f"«В очередь» сбрасывает курсор, но счётчики оставляет: "
    f"постов {state['total_items']}, статус {state['status']}",
)
check(
    db_stats.reset_tag_stats(["маникюр"]) == 1,
    "reset_tag_stats обнуляет счётчики одного тега",
)
state = db_stats.tag_state("маникюр")
check(
    int(state["total_items"]) == 0
    and int(state["total_pages"]) == 0
    and int(state["passes_done"]) == 0,
    f"Постов/Страниц/Проходов стали нулями: "
    f"{state['total_items']}/{state['total_pages']}/{state['passes_done']}",
)
check(
    state["status"] == "pending" and not state["next_from"],
    "обнуление счётчиков не меняет состояние и курсор тега",
)

db_stats.bump_tag("маникюр", items=5, pages=1)
db_stats.finish_tag("маникюр")
check(
    db_stats.reset_tag_stats(only_unfinished=True) == 0
    and int(db_stats.tag_state("маникюр")["total_items"]) == 5,
    "у собранных тегов счётчики не обнуляются (only_unfinished)",
)

# та же операция из консоли: python src/state_db.py --reset-stats
real_db_cls = state_db.DB
state_db.DB = lambda *args, **kwargs: db_stats  # main() поднимает свою базу — подменяем
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_code = state_db.main(["--reset-stats"])
finally:
    state_db.DB = real_db_cls
check(
    cli_code == 0 and "обнулено счётчиков" in buf.getvalue(),
    f"консольный --reset-stats обнуляет счётчики тегов: {buf.getvalue().splitlines()[0]!r}",
)
check(
    int(db_stats.tag_state("маникюр")["total_items"]) == 0,
    "после --reset-stats у тега нули",
)

# 14. «Обнулить счётчики» двигает и отсчёт карточки «Постов в базе»
db_mark = DB(tmp / "mark.sqlite3")
db_mark.add_posts([post("-9_9", "Объявление до обнуления")], "маникюр", stats={})
db_mark.bump_tag("маникюр", items=1, pages=1)
fresh_db = db_mark.stats()
check(
    fresh_db["posts"] == 1 and fresh_db["posts_since_reset"] == 1
    and fresh_db["tag_links_since_reset"] == 1 and fresh_db["counter_reset_at"] == 0,
    f"без обнуления карточка показывает всю базу: {fresh_db}",
)
check(db_mark.reset_tag_stats(["маникюр"]) == 1, "счётчики тега обнулены (та же кнопка)")
zeroed = db_mark.stats()
check(
    zeroed["posts"] == 1 and zeroed["posts_since_reset"] == 0
    and zeroed["tag_links_since_reset"] == 0 and zeroed["counter_reset_at"] > 0,
    f"карточка «Постов в базе» начинается с нуля, а в базе всё на месте: {zeroed}",
)
check(
    zeroed["posts_last_hour"] == 0,
    f"«за последний час» не считает посты, собранные до обнуления: "
    f"{zeroed['posts_last_hour']}",
)
check(db_mark.count_posts() == 1, "посты из базы не удалены — обнуляются только цифры")
db_mark.add_posts([post("-9_10", "Объявление после обнуления")], "маникюр", stats={})
grown = db_mark.stats()
check(
    grown["posts_since_reset"] == 1 and grown["posts"] == 2
    and grown["tag_links_since_reset"] == 1 and grown["posts_last_hour"] == 1,
    f"после обнуления считаются только новые посты: {grown}",
)
check(
    db_mark.reset_tag_stats(["нет-такого-тега"]) == 0
    and db_mark.stats()["posts_since_reset"] == 1,
    "обнулять нечего (тега нет) — отсчёт карточки не сдвигается",
)
check(db_mark.counter_mark()["posts"] == 1, "в базе лежит отметка отсчёта: где и сколько")

shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ПОВТОРЫ И СКОРОСТЬ РАБОТАЮТ")
raise SystemExit(1 if FAILED else 0)
