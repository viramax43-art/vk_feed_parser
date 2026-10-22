"""Состояние парсера: локальная SQLite (WAL) с постами, тегами, аккаунтами и журналом.

База пишется из нескольких потоков: у каждого своё соединение, мутации
транзакционные. Курсор тега (next_from) хранится рядом с окнами времени, чтобы
прерванный прогон продолжился с того же места.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from accounts import account_key as login_key
from paths import DATA_DIR, STATE_DB

DB_PATH = STATE_DB

# Запись повторяем, если базу держит другое соединение (воркер, панель, экспорт).
_LOCK_RETRIES = 5
_LOCK_PAUSE_SEC = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    query TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT,
    next_from TEXT,
    total_items INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL DEFAULT 0,
    passes_done INTEGER NOT NULL DEFAULT 0,
    -- Чем искать этот тег, если настройка tag_query = "auto": "hash" или "word".
    -- Пусто — авто-режим ещё не проверял тег (следующий прогон проверит).
    query_mode TEXT NOT NULL DEFAULT '',
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
    text_norm TEXT NOT NULL DEFAULT '',
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

-- Отметка «Обнулить счётчики»: с какого момента панель и окно считают посты и
-- ссылки. Строка всегда одна (id = 1). Данные не трогаются — обнуляются только
-- цифры (см. reset_tag_stats): посты, привязки и экспорт остаются как есть.
CREATE TABLE IF NOT EXISTS counter_mark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reset_at REAL NOT NULL DEFAULT 0,
    posts INTEGER NOT NULL DEFAULT 0,
    tag_links INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> float:
    return time.time()


def _is_locked(exc: BaseException) -> bool:
    """True — SQLite отказал из-за занятой базы («database is locked»/busy)."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _retry_locked(action, attempts: int = _LOCK_RETRIES, pause: float = _LOCK_PAUSE_SEC):
    """Повторяет действие, пока база занята другим соединением.

    BEGIN IMMEDIATE + busy_timeout закрывают почти все «database is locked», но
    если блокировку держит кто-то ещё (панель читает, экспорт пишет), транзакция
    повторяется: раньше такая ошибка вылетала воркеру как «сбой на окне» и окно
    шло по кругу повторов.
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc) or attempt >= max(1, attempts):
                raise
            time.sleep(pause * attempt)


_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_TAG_RE = re.compile(r"[#@]\w+")
_NOTEXT_RE = re.compile(r"[^0-9a-z\u0430-\u044f\u0451]+")


def norm_text(text: str) -> str:
    """Текст поста для сравнения дублей: без ссылок, хештегов и знаков.

    «#ногти #маникюр» и «🔥 #ногти #маникюр» дадут пустую строку — такой пост
    дубликатом не считается, его отсеивает min_text_chars.
    """
    clean = _LINK_RE.sub(" ", text or "")
    clean = _TAG_RE.sub(" ", clean)
    return _NOTEXT_RE.sub(" ", clean.lower()).strip()


def _backfill_text_norm(con: sqlite3.Connection) -> int:
    """Заполняет text_norm у уже собранных постов (одноразово при апгрейде базы).

    Без этого дедупликация видит только новые посты: у старых колонка пустая.
    """
    rows = con.execute(
        "SELECT item_key, text FROM posts WHERE text_norm = '' AND text != ''"
    ).fetchall()
    if not rows:
        return 0
    pairs = [(norm_text(r["text"]), r["item_key"]) for r in rows]
    con.execute("BEGIN")
    try:
        con.executemany("UPDATE posts SET text_norm = ? WHERE item_key = ?", pairs)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(pairs)


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
    tag_columns = {row["name"] for row in con.execute("PRAGMA table_info(tags)").fetchall()}
    if "query_mode" not in tag_columns:
        # Авто-режим тега (settings tag_query = "auto"): выбранное «хештег/слово»
        # живёт в базе, чтобы следующие прогоны не тратили пробные запросы.
        con.execute("ALTER TABLE tags ADD COLUMN query_mode TEXT NOT NULL DEFAULT ''")
    post_columns = {row["name"] for row in con.execute("PRAGMA table_info(posts)").fetchall()}
    if "text_norm" not in post_columns:
        # Колонку добавляем до создания её индекса: в SCHEMA индекса нет именно
        # потому, что на старой базе колонки ещё нет и executescript бы упал.
        con.execute("ALTER TABLE posts ADD COLUMN text_norm TEXT NOT NULL DEFAULT ''")
        filled = _backfill_text_norm(con)
        if filled:
            print(f"база обновлена: text_norm заполнен у {filled} постов")
    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_text_norm ON posts(text_norm)")
    merged = _migrate_account_logins(con)
    if merged:
        print(f"база обновлена: строки аккаунтов слиты по телефону ({merged})")


def _migrate_account_logins(con: sqlite3.Connection) -> int:
    """Сливает строки аккаунтов, записанные в другом написании телефона.

    Вход в VK сохранял статус под «+79000000000», а прогон и панель считают
    аккаунт по цифрам («79000000000»): в базе появлялись двойники, и панель
    показывала аккаунтов вдвое больше, чем лежит в accounts.txt.
    """
    rows = con.execute("SELECT login FROM accounts").fetchall()
    moved = 0
    for row in rows:
        old = str(row["login"] or "")
        key = login_key(old)
        if not key or key == old:
            continue

        def move(old: str = old, key: str = key) -> None:
            con.execute("BEGIN IMMEDIATE")
            try:
                # Счётчики и статус переносим в строку с нормальным ключом.
                con.execute(
                    "INSERT INTO accounts(login, user_id, proxy, status, total_items,"
                    " last_error, updated_at, kind, cooldown_until, last_ok_at, fail_count)"
                    " SELECT ?, user_id, proxy, status, total_items, last_error, updated_at,"
                    " kind, cooldown_until, last_ok_at, fail_count FROM accounts WHERE login = ?"
                    " ON CONFLICT(login) DO UPDATE SET"
                    "   total_items = accounts.total_items + excluded.total_items,"
                    "   fail_count = accounts.fail_count + excluded.fail_count,"
                    "   updated_at = MAX(accounts.updated_at, excluded.updated_at)",
                    (key, old),
                )
                con.execute("DELETE FROM accounts WHERE login = ?", (old,))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

        _retry_locked(move)
        moved += 1
    return moved


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    _ensure_schema(con, Path(path))
    return con


_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def _ensure_schema(con: sqlite3.Connection, path: Path) -> None:
    """Схема, миграции и WAL — один раз на процесс, а не на каждое соединение.

    Соединение открывает каждый воркер, панель и экспорт: раньше DDL и смена
    журнала выполнялись при каждом connect(), то есть сотни лишних запросов в
    schema-lock и лишние «database is locked» на ровном месте.
    """
    key = str(path)
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        con.executescript(SCHEMA)
        _migrate(con)
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # Базу держит другой процесс: WAL включён им или включится позже.
                pass
        _schema_ready.add(key)


class DB:
    """Потокобезопасная обёртка: своё соединение на поток, мутации транзакционные."""

    def __init__(
        self,
        path: Path = DB_PATH,
        *,
        dedupe: str = "off",
        min_text_chars: int = 0,
    ) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        # Политику дублей задаёт прогон (settings.json): "off" — как раньше,
        # "tag" — не писать текст, уже собранный по этому тегу, "global" — по базе.
        self.dedupe = dedupe if dedupe in ("off", "tag", "global") else "off"
        self.min_text_chars = max(0, int(min_text_chars or 0))

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

    def tag_query_mode(self, query: str) -> str:
        """Чем искать тег по авто-режиму: "hash"/"word" ("" — ещё не выбран)."""
        row = self.con.execute(
            "SELECT query_mode FROM tags WHERE query = ?", (query,)
        ).fetchone()
        mode = str((row["query_mode"] if row else "") or "")
        return mode if mode in ("hash", "word") else ""

    def set_tag_query_mode(self, query: str, mode: str) -> None:
        """Запоминает выбор авто-режима для тега (пробные запросы больше не нужны)."""
        mode = str(mode or "")
        if mode not in ("hash", "word"):
            return
        self.ensure_tag(query)
        self.con.execute(
            "UPDATE tags SET query_mode = ?, updated_at = ? WHERE query = ?",
            (mode, _now(), query),
        )

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

    def reset_tag_stats(
        self, queries: Optional[Iterable[str]] = None, only_unfinished: bool = False
    ) -> int:
        """Обнуляет накопленные счётчики тега: Постов/Страниц/Проходов → 0.

        Отдельно от reset_tags («В очередь»): тот возвращает тег к сбору (статус,
        курсор, next_from), а цифры остаются историей за всё время — они только растут
        (bump_tag). Здесь наоборот: обнуляются только счётчики, а статус, курсор и
        next_from не трогаются. В панели и в окне это кнопка «Обнулить счётчики»,
        из консоли — python src/state_db.py --reset-stats.

        Заодно ставится отметка отсчёта (counter_mark): карточка «Постов в базе» и
        цифры «ссылок»/«за час» в панели и в окне начинают считать с нуля — как и
        счётчики тегов. Посты и привязки остаются в базе: «всего в базе N» в
        подписи карточки, экспорт и дедупликация ничего не теряют.
        """
        where: list[str] = []
        params: list = []
        if queries:
            marks = ",".join("?" for _ in queries)
            where.append(f"query IN ({marks})")
            params.extend(queries)
        if only_unfinished:
            where.append("status != 'finished'")
        sql = (
            "UPDATE tags SET total_items = 0, total_pages = 0, passes_done = 0,"
            " updated_at = ?"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        cur = self.con.execute(sql, (_now(), *params))
        count = int(cur.rowcount or 0)
        if count:
            # Обнулять было что — сдвигаем и отсчёт карточек (иначе «счётчики
            # обнулены», а «Постов в базе» всё равно 10894).
            self.set_counter_mark()
        return count

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
        self,
        posts: Iterable[dict],
        tag: str,
        pass_no: int = 1,
        page: int = 0,
        stats: Optional[dict] = None,
    ) -> int:
        """Пишет страницу постов одной транзакцией; возвращает число новых для тега.

        Повторы по тексту (settings dedupe_posts) и мусор короче min_text_chars
        не пишутся: счётчики уходят в stats['dupes'] / stats['junk'].

        Транзакция берётся сразу на запись и повторяется, если базу держит другое
        соединение (панель или экспорт): раньше здесь вылетало
        «OperationalError: database is locked» и окно уходило в повторы.
        """
        # Материализуем вход: повтор транзакции не должен видеть пустой список.
        rows = list(posts)
        return _retry_locked(
            lambda: self._add_posts_page(rows, tag, pass_no, page, stats)
        )

    def _add_posts_page(
        self,
        posts: Iterable[dict],
        tag: str,
        pass_no: int = 1,
        page: int = 0,
        stats: Optional[dict] = None,
    ) -> int:
        """Одна попытка записи страницы: BEGIN IMMEDIATE, COMMIT или ROLLBACK."""
        rows = [p for p in posts if isinstance(p, dict) and p.get("key")]
        if not rows:
            return 0
        now = _now()
        new_for_tag = 0
        dupes = 0
        junk = 0
        con = self.con
        # BEGIN IMMEDIATE: блокировку записи берём сразу. С обычным BEGIN
        # транзакция начиналась как читающая, и первый INSERT падал
        # «database is locked», не дожидаясь busy_timeout.
        con.execute("BEGIN IMMEDIATE")
        try:
            for post in rows:
                key = str(post["key"])
                author = post.get("author") or {}
                norm = norm_text(post.get("text") or "")
                if self.min_text_chars and len(norm) < self.min_text_chars:
                    junk += 1
                    continue
                if self.dedupe != "off" and norm and self._text_seen(norm, tag):
                    dupes += 1
                    continue
                con.execute(
                    "INSERT INTO posts(item_key, owner_id, post_id, url, date, date_str, text,"
                    " author_name, author_kind, author_url, likes, comments, reposts, views,"
                    " attachments, photos, videos, docs, audios, first_tag, text_norm,"
                    " seen_at, payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(item_key) DO UPDATE SET"
                    "   likes=excluded.likes, comments=excluded.comments,"
                    "   reposts=excluded.reposts, views=excluded.views,"
                    "   text=excluded.text, date=excluded.date, date_str=excluded.date_str,"
                    "   text_norm=excluded.text_norm"
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
                        norm,
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
        if isinstance(stats, dict):
            stats["dupes"] = int(stats.get("dupes") or 0) + dupes
            stats["junk"] = int(stats.get("junk") or 0) + junk
        return new_for_tag

    def _text_seen(self, norm: str, tag: str) -> bool:
        """Есть ли уже пост с таким текстом — по этому тегу или по всей базе."""
        if self.dedupe == "global":
            row = self.con.execute(
                "SELECT 1 FROM posts WHERE text_norm = ? LIMIT 1", (norm,)
            ).fetchone()
        else:
            row = self.con.execute(
                "SELECT 1 FROM posts p JOIN tag_posts t ON t.item_key = p.item_key"
                " WHERE p.text_norm = ? AND t.tag = ? LIMIT 1",
                (norm, tag),
            ).fetchone()
        return row is not None

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
        """Строки для Excel: один пост = одна строка, теги собраны через запятую.

        Раньше строка шла на пару «пост × тег»: пост, найденный по двум
        хештегам, выгружался дважды (отсюда же «200 постов, 262 строки»).
        """
        sql = (
            "SELECT p.*, GROUP_CONCAT(DISTINCT t.tag) AS tags, MIN(t.tag) AS tag"
            " FROM posts p JOIN tag_posts t ON t.item_key = p.item_key"
        )
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
        sql += " GROUP BY p.item_key ORDER BY p.date DESC, p.item_key DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for row in self.con.execute(sql, params).fetchall():
            item = dict(row)
            item["tag"] = item.get("tags") or item.get("tag") or ""
            out.append(item)
        return out

    def find_text_dupes(self, limit: int = 0, keep: str = "first") -> list[dict]:
        """Посты, чей текст уже встречался раньше, — для чистки старой базы.

        keep="first" — оставляем самый ранний пост с этим текстом, "likes" —
        самый популярный. Возвращает то, что надо удалить.
        """
        rows = self.con.execute(
            "SELECT item_key, text_norm, date, likes, first_tag FROM posts"
            " WHERE text_norm != '' ORDER BY text_norm, date, item_key"
        ).fetchall()
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["text_norm"], []).append(dict(row))
        out: list[dict] = []
        for items in groups.values():
            if len(items) < 2:
                continue
            if keep == "likes":
                keeper = max(items, key=lambda i: (int(i.get("likes") or 0), -int(i["date"] or 0)))
            else:
                keeper = min(items, key=lambda i: (int(i["date"] or 0), str(i["item_key"])))
            out.extend(i for i in items if i["item_key"] != keeper["item_key"])
        out.sort(key=lambda i: (i["text_norm"], str(i["item_key"])))
        return out[:limit] if limit and limit > 0 else out

    def drop_posts(self, keys: Iterable[str]) -> int:
        """Удаляет посты и их привязки к тегам (чистка дублей)."""
        keys = [str(k) for k in keys]
        if not keys:
            return 0
        con = self.con
        removed = 0
        con.execute("BEGIN IMMEDIATE")
        try:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                con.execute(f"DELETE FROM tag_posts WHERE item_key IN ({marks})", chunk)
                cur = con.execute(f"DELETE FROM posts WHERE item_key IN ({marks})", chunk)
                removed += int(cur.rowcount or 0)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return removed


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

    def revive_failed_jobs(self, tags: Iterable[str], since: int = 0, until: int = 0) -> int:
        """Возвращает в очередь окна, брошенные после ошибок (например истёк токен).

        Без этого «брошенное» окно мёртвое навсегда: open_jobs берёт только
        pending/running, а планировщик собранным считает только done. Так на
        истёкшем токене разом пропали целые сутки — окна ушли в failed со
        счётчиком попыток и больше не перезапрашивались.
        """
        tags = list(tags)
        if not tags:
            return 0
        marks = ",".join("?" for _ in tags)
        sql = (
            f"UPDATE jobs SET status = 'pending', attempts = 0, cursor = '',"
            f" reason = 'revived', updated_at = ?"
            f" WHERE status = 'failed' AND tag IN ({marks})"
        )
        params: list = [_now(), *tags]
        if since:
            sql += " AND end_time > ?"
            params.append(int(since))
        if until:
            sql += " AND start_time < ?"
            params.append(int(until))
        cur = self.con.execute(sql, params)
        return int(cur.rowcount or 0)

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

    def drop_jobs(self, job_ids: Iterable[int], reason: str = "out_of_period") -> int:
        """Снимает конкретные окна с очереди (status = 'dropped').

        Нужно для остатков прошлого запуска, оказавшихся вне запрошенного
        периода: добирать их нельзя, но и оставлять pending нельзя — иначе они
        вернутся в следующий запуск.
        """
        ids = [int(i) for i in job_ids]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = self.con.execute(
            f"UPDATE jobs SET status = 'dropped', reason = ?, updated_at = ?"
            f" WHERE id IN ({marks}) AND status IN ('pending','running')",
            (reason, _now(), *ids),
        )
        return int(cur.rowcount or 0)

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"status", "cursor", "attempts", "seen", "new", "pages", "reason"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in updates)
        self.con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*updates.values(), int(job_id)))

    def covered_windows(self, tag: str, max_seen: int, since: int = 0) -> list[tuple[int, int]]:
        """Отдельные собранные окна (не слипшиеся) тега: окно закрыто, вернуло
        посты, но меньше порога деления (значит, не обрезано).

        since — брать только окна, заканчивающиеся позже этого времени: так
        выбирается свежий хвост, который перепроверяем (лента меняется).
        """
        rows = self.con.execute(
            # seen = 0 с причиной end_of_feed/no_new_posts — лента пройдена до конца,
            # перезапрашивать такое окно не нужно
            "SELECT start_time, end_time FROM jobs WHERE tag = ? AND status = 'done'"
            " AND seen < ? AND (seen > 0 OR reason IN ('end_of_feed', 'no_new_posts'))"
            " AND end_time > ? ORDER BY start_time",
            (tag, int(max_seen), int(since)),
        ).fetchall()
        return [(int(r["start_time"]), int(r["end_time"])) for r in rows]

    def covered_intervals(self, tag: str, max_seen: int) -> list[tuple[int, int]]:
        """Отрезки времени, которые уже собраны полностью (соседние окна слиты)."""
        merged: list[list[int]] = []
        for lo, hi in self.covered_windows(tag, max_seen):
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

        Логин приводится к общему ключу (accounts.account_key): вход в VK писал
        «+79000000000», а прогон — «79000000000», из-за чего в базе появлялись
        двойники и панель показывала лишние аккаунты.
        """
        login = login_key(login)
        if not login:
            return
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

    def delete_accounts(self, logins: list[str]) -> int:
        """Удаляет строки аккаунтов из базы. Возвращает число удалённых строк."""
        keys = [login_key(login) for login in logins if login]
        keys = [key for key in keys if key]
        if not keys:
            return 0
        marks = ",".join("?" for _ in keys)
        cur = self.con.execute(f"DELETE FROM accounts WHERE login IN ({marks})", keys)
        return int(cur.rowcount or 0)

    def list_accounts(self) -> list[dict]:
        rows = self.con.execute("SELECT * FROM accounts ORDER BY login").fetchall()
        return [dict(r) for r in rows]

    def add_account_items(self, login: str, count: int) -> None:
        """Прибавляет аккаунту собранные посты; сам аккаунт создаём, если его ещё нет.

        Без апсерта первый запрос нового аккаунта терялся: строка в accounts
        появлялась позже (её создаёт mark_account) и UPDATE не находил цель.
        Ключ — общий (accounts.account_key), иначе «+7…» и «7…» — две строки.
        """
        login = login_key(login)
        if not login:
            return
        now = _now()
        self.con.execute(
            "INSERT INTO accounts(login, total_items, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(login) DO UPDATE SET total_items = total_items + ?, updated_at = ?",
            (login, int(count), now, int(count), now),
        )

    def account_state(self, login: str) -> dict:
        row = self.con.execute(
            "SELECT * FROM accounts WHERE login = ?", (login_key(login),)
        ).fetchone()
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

    def counter_mark(self) -> dict:
        """Отметка «Обнулить счётчики»: когда обнуляли и какие цифры были тогда.

        Отметки нет — zeros: карточки в панели и в окне показывают всё, что лежит
        в базе (как было до появления обнуления счётчиков).
        """
        row = self.con.execute(
            "SELECT reset_at, posts, tag_links FROM counter_mark WHERE id = 1"
        ).fetchone()
        if not row:
            return {"reset_at": 0.0, "posts": 0, "tag_links": 0}
        return {
            "reset_at": float(row["reset_at"] or 0.0),
            "posts": int(row["posts"] or 0),
            "tag_links": int(row["tag_links"] or 0),
        }

    def set_counter_mark(self) -> dict:
        """Ставит отметку по текущему состоянию базы: карточки начнут считать с нуля.

        Сама база не трогается: отметка — это «нулевая точка» для цифр. Так кнопка
        «Обнулить счётчики» обнуляет и «Постов в базе», не удаляя ни одного поста
        (экспорт и дедупликация работают по всей базе, как раньше).
        """
        now = _now()
        posts = int(self.con.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"])
        links = int(self.con.execute("SELECT COUNT(*) AS n FROM tag_posts").fetchone()["n"])
        self.con.execute(
            "INSERT INTO counter_mark(id, reset_at, posts, tag_links) VALUES(1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET reset_at = excluded.reset_at,"
            " posts = excluded.posts, tag_links = excluded.tag_links",
            (now, posts, links),
        )
        return {"reset_at": now, "posts": posts, "tag_links": links}

    def stats(self) -> dict:
        """Цифры для панели, окна и консоли (state_db.py --stats, export_xlsx.py).

        «posts»/«tag_links» — всё, что лежит в базе; «posts_since_reset» и
        «tag_links_since_reset» — с последнего «Обнулить счётчики» (counter_mark):
        именно их показывает карточка «Постов в базе», а счётчик «за час» не
        считает посты, собранные до обнуления.
        """
        mark = self.counter_mark()
        posts = self.con.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
        links = self.con.execute("SELECT COUNT(*) AS n FROM tag_posts").fetchone()["n"]
        tags = self.con.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE status = 'finished'"
        ).fetchone()["n"]
        total_tags = self.con.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"]
        accounts = self.con.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        fresh_from = _now() - 3600
        if mark["reset_at"] > 0:
            fresh_from = max(fresh_from, float(mark["reset_at"]))
        fresh = self.con.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE seen_at >= ?", (fresh_from,)
        ).fetchone()["n"]
        return {
            "posts": int(posts),
            "tag_links": int(links),
            "posts_since_reset": max(0, int(posts) - int(mark["posts"])),
            "tag_links_since_reset": max(0, int(links) - int(mark["tag_links"])),
            "counter_reset_at": float(mark["reset_at"]),
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
    parser.add_argument(
        "--reset-stats",
        action="store_true",
        help="обнулить счётчики тегов (Постов/Страниц/Проходов; статусы и курсоры не тронутся)",
    )
    parser.add_argument("--only-unfinished", action="store_true")
    parser.add_argument(
        "--drop-text-dupes",
        action="store_true",
        help="повторы по тексту: без --apply только показывает, что нашлось",
    )
    parser.add_argument("--apply", action="store_true", help="удалить найденные повторы")
    parser.add_argument("--dupe-limit", type=int, default=0, help="сколько повторов взять (0 — все)")
    parser.add_argument(
        "--keep",
        choices=("first", "likes"),
        default="first",
        help="какой пост из повторов оставить: самый ранний или самый популярный",
    )
    args = parser.parse_args(argv)

    db = DB()
    if args.reset:
        n = db.reset_tags(only_unfinished=args.only_unfinished)
        print(f"сброшено тегов: {n}")
    if args.reset_stats:
        n = db.reset_tag_stats(only_unfinished=args.only_unfinished)
        print(
            f"обнулено счётчиков у тегов: {n}"
            " (Постов/Страниц/Проходов → 0; статусы и курсоры как были)"
        )
        if n:
            mark = db.counter_mark()
            print(
                "отметка отсчёта: карточки «Постов в базе» и «ссылок» в панели и в окне"
                f" считают с нуля — в базе при этом {mark['posts']} постов"
                f" и {mark['tag_links']} ссылок"
            )
    if args.drop_text_dupes:
        dupes = db.find_text_dupes(limit=args.dupe_limit, keep=args.keep)
        total = len(dupes)
        print(f"повторов по тексту найдено: {total} (оставляю: {args.keep})")
        for row in dupes[:10]:
            print(
                f"  {row['item_key']:<24} {time.strftime('%d.%m %H:%M', time.localtime(int(row['date'] or 0)))}"
                f" [{row.get('first_tag') or '—'}] {(row.get('text_norm') or '')[:70]!r}"
            )
        if total > 10:
            print(f"  ... и ещё {total - 10}")
        if args.apply:
            removed = db.drop_posts([row["item_key"] for row in dupes])
            print(f"удалено постов: {removed} (вместе с их привязками к тегам)")
        else:
            print("это примерка: чтобы удалить, добавьте --apply")
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
