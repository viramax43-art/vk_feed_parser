"""Состояние парсера: локальная SQLite (WAL) с постами, тегами, аккаунтами и журналом.

База пишется из нескольких потоков: у каждого своё соединение, мутации
транзакционные. Курсор тега (next_from) хранится рядом с окнами времени, чтобы
прерванный прогон продолжился с того же места.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from paths import DATA_DIR, STATE_DB

DB_PATH = STATE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    query TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT,
    next_from TEXT,
    total_items INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL DEFAULT 0,
    passes_done INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_run_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    item_key TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL DEFAULT 0,
    post_id INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    date INTEGER NOT NULL DEFAULT 0,
    date_str TEXT,
    text TEXT,
    author_name TEXT,
    author_kind TEXT,
    author_url TEXT,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    reposts INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    attachments TEXT,
    photos INTEGER NOT NULL DEFAULT 0,
    videos INTEGER NOT NULL DEFAULT 0,
    docs INTEGER NOT NULL DEFAULT 0,
    audios INTEGER NOT NULL DEFAULT 0,
    first_tag TEXT,
    seen_at REAL NOT NULL DEFAULT 0,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date);
CREATE INDEX IF NOT EXISTS idx_posts_owner ON posts(owner_id);

CREATE TABLE IF NOT EXISTS tag_posts (
    tag TEXT NOT NULL,
    item_key TEXT NOT NULL,
    pass_no INTEGER NOT NULL DEFAULT 1,
    page INTEGER NOT NULL DEFAULT 0,
    found_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(tag, item_key)
);
CREATE INDEX IF NOT EXISTS idx_tag_posts_key ON tag_posts(item_key);

CREATE TABLE IF NOT EXISTS accounts (
    login TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 0,
    proxy TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    total_items INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_ok_at REAL NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    account TEXT,
    tag TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- Окна времени по тегу: единица работы для потоков (тег режется на куски
-- [start_time, end_time), куски раздаются всем аккаунтам параллельно).
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    tag TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    cursor TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    seen INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(tag, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, tag);
"""


def _now() -> float:
    return time.time()


def _migrate(con: sqlite3.Connection) -> None:
    """Догоняет схему у старых баз: добавляет недостающие колонки без потери данных."""
    columns = {row["name"] for row in con.execute("PRAGMA table_info(accounts)").fetchall()}
    additions = {
        "kind": "TEXT NOT NULL DEFAULT ''",
        "cooldown_until": "REAL NOT NULL DEFAULT 0",
        "last_ok_at": "REAL NOT NULL DEFAULT 0",
        "fail_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in additions.items():
        if name not in columns:
            con.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    con.executescript(SCHEMA)
    _migrate(con)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    return con


class DB:
    """Потокобезопасная обёртка: своё соединение на поток, мутации транзакционные."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = connect(self.path)
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.con = None

    # Теги
    def ensure_tag(self, query: str) -> None:
        now = _now()
        self.con.execute(
            "INSERT INTO tags(query, status, first_seen_at, updated_at) "
            "VALUES(?, 'pending', ?, ?) ON CONFLICT(query) DO NOTHING",
            (query, now, now),
        )

    def tag_state(self, query: str) -> dict:
        self.ensure_tag(query)
        row = self.con.execute("SELECT * FROM tags WHERE query = ?", (query,)).fetchone()
        return dict(row) if row else {}

    def set_tag_cursor(self, query: str, **fields) -> None:
        self.ensure_tag(query)
        allowed = {
            "next_from",
            "status",
            "stop_reason",
            "total_items",
            "total_pages",
            "passes_done",
            "last_run_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in updates)
        self.con.execute(f"UPDATE tags SET {cols} WHERE query = ?", (*updates.values(), query))

    def finish_tag(self, query: str, status: str = "finished", reason: str = "") -> None:
        self.set_tag_cursor(query, status=status, stop_reason=reason, last_run_at=_now())

    def bump_tag(
        self,
        query: str,
        *,
        items: int = 0,
        pages: int = 0,
        next_from: Optional[str] = None,
        pass_no: Optional[int] = None,
    ) -> None:
        state = self.tag_state(query)
        fields: dict = {
            "total_items": int(state.get("total_items") or 0) + int(items),
            "total_pages": int(state.get("total_pages") or 0) + int(pages),
            "last_run_at": _now(),
            "status": "running",
        }
        if next_from is not None:
            fields["next_from"] = next_from
        if pass_no is not None:
            fields["passes_done"] = max(pass_no, int(state.get("passes_done") or 0))
        self.set_tag_cursor(query, **fields)

    def reset_tags(
        self, queries: Optional[Iterable[str]] = None, only_unfinished: bool = False
    ) -> int:
        where: list[str] = []
        params: list = []
        if queries:
            marks = ",".join("?" for _ in queries)
            where.append(f"query IN ({marks})")
            params.extend(queries)
        if only_unfinished:
            where.append("status != 'finished'")
        sql = (
            "UPDATE tags SET status = 'pending', stop_reason = NULL,"
            " next_from = NULL, updated_at = ?"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        cur = self.con.execute(sql, (_now(), *params))
        return int(cur.rowcount or 0)

    def pending_tags(
        self, queries: Iterable[str], retry_unfinished: bool = False
    ) -> list[str]:
        out: list[str] = []
        for query in queries:
            state = self.tag_state(query)
            if state.get("status") == "finished" and not retry_unfinished:
                continue
            out.append(query)
        return out

    def all_tags(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM tags ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


    # Посты
    def add_posts(
        self, posts: Iterable[dict], tag: str, pass_no: int = 1, page: int = 0
    ) -> int:
        """Пишет страницу постов одной транзакцией; возвращает число новых для тега."""
        rows = [p for p in posts if isinstance(p, dict) and p.get("key")]
        if not rows:
            return 0
        now = _now()
        new_for_tag = 0
        con = self.con
        con.execute("BEGIN")
        try:
            for post in rows:
                key = str(post["key"])
                author = post.get("author") or {}
                con.execute(
                    "INSERT INTO posts(item_key, owner_id, post_id, url, date, date_str, text,"
                    " author_name, author_kind, author_url, likes, comments, reposts, views,"
                    " attachments, photos, videos, docs, audios, first_tag, seen_at, payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(item_key) DO UPDATE SET"
                    "   likes=excluded.likes, comments=excluded.comments,"
                    "   reposts=excluded.reposts, views=excluded.views,"
                    "   text=excluded.text, date=excluded.date, date_str=excluded.date_str"
                    " WHERE excluded.likes != posts.likes"
                    "    OR excluded.comments != posts.comments"
                    "    OR excluded.reposts != posts.reposts"
                    "    OR excluded.views != posts.views",
                    (
                        key,
                        int(post.get("owner_id") or 0),
                        int(post.get("post_id") or 0),
                        post.get("url") or "",
                        int(post.get("date") or 0),
                        post.get("date_str") or "",
                        post.get("text") or "",
                        author.get("name") or "",
                        author.get("kind") or "",
                        author.get("url") or "",
                        int(post.get("likes") or 0),
                        int(post.get("comments") or 0),
                        int(post.get("reposts_count") or 0),
                        int(post.get("views") or 0),
                        json.dumps(post.get("attachments") or [], ensure_ascii=False),
                        int(post.get("photos") or 0),
                        int(post.get("videos") or 0),
                        int(post.get("docs") or 0),
                        int(post.get("audios") or 0),
                        tag,
                        now,
                        json.dumps(post, ensure_ascii=False),
                    ),
                )
                cur = con.execute(
                    "INSERT INTO tag_posts(tag, item_key, pass_no, page, found_at)"
                    " VALUES(?,?,?,?,?) ON CONFLICT(tag, item_key) DO NOTHING",
                    (tag, key, pass_no, page, now),
                )
                if cur.rowcount:
                    new_for_tag += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return new_for_tag

    def count_posts(self, tag: Optional[str] = None) -> int:
        if tag:
            row = self.con.execute(
                "SELECT COUNT(*) AS n FROM tag_posts WHERE tag = ?", (tag,)
            ).fetchone()
        else:
            row = self.con.execute("SELECT COUNT(*) AS n FROM posts").fetchone()
        return int(row["n"] if row else 0)

    def top_tags(self, limit: int = 20) -> list[dict]:
        rows = self.con.execute(
            "SELECT t.tag, COUNT(*) AS n FROM tag_posts t"
            " GROUP BY t.tag ORDER BY n DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_rows(
        self,
        tag: Optional[str] = None,
        since: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        sql = "SELECT p.*, t.tag FROM posts p JOIN tag_posts t ON t.item_key = p.item_key"
        where: list[str] = []
        params: list = []
        if tag:
            where.append("t.tag = ?")
            params.append(tag)
        if since:
            where.append("p.seen_at >= ?")
            params.append(float(since))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.date DESC, p.item_key DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]


    def tag_max_date(self, tag: str) -> int:
        """Дата самого свежего поста тега — с неё начинается повторный проход."""
        row = self.con.execute(
            "SELECT MAX(p.date) AS d FROM posts p"
            " JOIN tag_posts t ON t.item_key = p.item_key WHERE t.tag = ?",
            (tag,),
        ).fetchone()
        return int(row["d"] or 0) if row else 0

    def count_tag_posts_between(self, tag: str, start: int, end: int) -> int:
        """Сколько постов тега уже есть в базе за [start, end) — для детекта пустой выдачи."""
        row = self.con.execute(
            "SELECT COUNT(*) AS n FROM posts p JOIN tag_posts t ON t.item_key = p.item_key"
            " WHERE t.tag = ? AND p.date >= ? AND p.date < ?",
            (tag, int(start), int(end)),
        ).fetchone()
        return int(row["n"] if row else 0)

    # Окна (jobs)
    def add_job(self, tag: str, start_time: int, end_time: int, round_no: int = 1) -> dict:
        """Создаёт окно (или возвращает существующее с теми же границами)."""
        self.con.execute(
            "INSERT INTO jobs(tag, start_time, end_time, round, status, updated_at)"
            " VALUES(?,?,?,?,'pending',?)"
            " ON CONFLICT(tag, start_time, end_time) DO NOTHING",
            (tag, int(start_time), int(end_time), int(round_no), _now()),
        )
        row = self.con.execute(
            "SELECT * FROM jobs WHERE tag = ? AND start_time = ? AND end_time = ?",
            (tag, int(start_time), int(end_time)),
        ).fetchone()
        return dict(row)

    def open_jobs(self, tags: Iterable[str]) -> list[dict]:
        """Незавершённые окна (прерванный прогон); running → pending."""
        tags = list(tags)
        if not tags:
            return []
        marks = ",".join("?" for _ in tags)
        self.con.execute(
            f"UPDATE jobs SET status = 'pending' WHERE status = 'running' AND tag IN ({marks})",
            tags,
        )
        rows = self.con.execute(
            f"SELECT * FROM jobs WHERE status = 'pending' AND tag IN ({marks})"
            " ORDER BY tag, end_time DESC",
            tags,
        ).fetchall()
        return [dict(r) for r in rows]

    def drop_open_jobs(self, tags: Iterable[str]) -> int:
        tags = list(tags)
        if not tags:
            return 0
        marks = ",".join("?" for _ in tags)
        cur = self.con.execute(
            f"UPDATE jobs SET status = 'dropped', updated_at = ?"
            f" WHERE status IN ('pending','running') AND tag IN ({marks})",
            (_now(), *tags),
        )
        return int(cur.rowcount or 0)

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"status", "cursor", "attempts", "seen", "new", "pages", "reason"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in updates)
        self.con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*updates.values(), int(job_id)))

    def covered_intervals(self, tag: str, max_seen: int) -> list[tuple[int, int]]:
        """Отрезки времени, которые уже собраны полностью: окно закрыто,
        вернуло посты, но меньше порога деления (значит, не обрезано)."""
        rows = self.con.execute(
            "SELECT start_time, end_time FROM jobs WHERE tag = ? AND status = 'done'"
            " AND seen > 0 AND seen < ? ORDER BY start_time",
            (tag, int(max_seen)),
        ).fetchall()
        merged: list[list[int]] = []
        for r in rows:
            lo, hi = int(r["start_time"]), int(r["end_time"])
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return [(a, b) for a, b in merged]

    def count_open_jobs(self, tag: str) -> int:
        row = self.con.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE tag = ? AND status IN ('pending','running')",
            (tag,),
        ).fetchone()
        return int(row["n"] if row else 0)

    # Аккаунты и события
    def mark_account(
        self,
        login: str,
        *,
        user_id: Optional[int] = None,
        proxy: str = "",
        status: str = "ready",
        error: str = "",
        kind: Optional[str] = None,
        cooldown_until: Optional[float] = None,
        touch_ok: bool = False,
        bump_fail: bool = False,
    ) -> None:
        """Обновляет состояние аккаунта.

        touch_ok — отметить успех (обнуляет счётчик сбоев), bump_fail —
        зафиксировать сбой, cooldown_until — до какого времени аккаунт в отлёжке,
        kind — источник входа (cookies или пароль).
        """
        now = _now()
        self.con.execute(
            "INSERT INTO accounts(login, status, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(login) DO NOTHING",
            (login, status, now),
        )
        sets = ["status = ?", "last_error = ?", "updated_at = ?"]
        params: list = [status, error or "", now]
        if user_id is not None:
            sets.append("user_id = ?")
            params.append(int(user_id))
        if proxy:
            sets.append("proxy = ?")
            params.append(proxy)
        if kind is not None:
            sets.append("kind = ?")
            params.append(kind)
        if cooldown_until is not None:
            sets.append("cooldown_until = ?")
            params.append(float(cooldown_until))
        if touch_ok:
            sets.append("last_ok_at = ?")
            params.append(now)
            sets.append("fail_count = 0")
        if bump_fail:
            sets.append("fail_count = fail_count + 1")
        self.con.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE login = ?",
            (*params, login),
        )

    def list_accounts(self) -> list[dict]:
        rows = self.con.execute("SELECT * FROM accounts ORDER BY login").fetchall()
        return [dict(r) for r in rows]

    def add_account_items(self, login: str, count: int) -> None:
        """Прибавляет аккаунту собранные посты; сам аккаунт создаём, если его ещё нет.

        Без апсерта первый запрос нового аккаунта терялся: строка в accounts
        появлялась позже (её создаёт mark_account) и UPDATE не находил цель.
        """
        now = _now()
        self.con.execute(
            "INSERT INTO accounts(login, total_items, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(login) DO UPDATE SET total_items = total_items + ?, updated_at = ?",
            (login, int(count), now, int(count), now),
        )

    def account_state(self, login: str) -> dict:
        row = self.con.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else {}

    def event(
        self,
        level: str,
        kind: str,
        message: str,
        account: str = "",
        tag: str = "",
    ) -> None:
        self.con.execute(
            "INSERT INTO events(created_at, level, kind, account, tag, message)"
            " VALUES(?,?,?,?,?,?)",
            (_now(), level, kind, account or None, tag or None, message),
        )

    def recent_events(self, limit: int = 30) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        posts = self.con.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
        links = self.con.execute("SELECT COUNT(*) AS n FROM tag_posts").fetchone()["n"]
        tags = self.con.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE status = 'finished'"
        ).fetchone()["n"]
        total_tags = self.con.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"]
        accounts = self.con.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        fresh = self.con.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE seen_at >= ?", (_now() - 3600,)
        ).fetchone()["n"]
        return {
            "posts": int(posts),
            "tag_links": int(links),
            "tags_finished": int(tags),
            "tags_total": int(total_tags),
            "accounts": int(accounts),
            "posts_last_hour": int(fresh),
        }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Состояние VK Feed Parser")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--tags", action="store_true")
    parser.add_argument("--events", type=int, default=0)
    parser.add_argument("--reset", action="store_true", help="сбросить статусы тегов")
    parser.add_argument("--only-unfinished", action="store_true")
    args = parser.parse_args(argv)

    db = DB()
    if args.reset:
        n = db.reset_tags(only_unfinished=args.only_unfinished)
        print(f"сброшено тегов: {n}")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    if args.tags:
        for row in db.all_tags():
            print(
                f"  {row['query']:<24} {row['status']:<10} "
                f"постов={row['total_items']:<6} страниц={row['total_pages']:<4} "
                f"next_from={'да' if row.get('next_from') else 'нет'} "
                f"{row.get('stop_reason') or ''}"
            )
    if args.events:
        for row in db.recent_events(args.events):
            print(f"  {row['created_at']:.0f} {row['level']} {row['kind']}: {row['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
