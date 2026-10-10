#!/usr/bin/env python3
"""Окно массового запуска: аккаунты, прогон, теги, события и лог.

Запуск:
    python src/gui.py              # окно массового запуска
    python app.py --gui            # то же самое из меню
    python src/gui.py --selftest   # собрать окно скрыто (Tk и база, без показа)

Прогон идёт отдельным процессом (src/run_tags.py), окно читает состояние из
data/parser.sqlite3, «Стоп» посылает процессу Ctrl+C. tkinter подключается в
main(): на Python без Tk модуль остаётся импортируемым, main() вернёт 3.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from account_pool import DEAD, HOLD, READY, normalize_status, status_label  # noqa: E402
from accounts import load_accounts, parse_account_line  # noqa: E402
from paths import (  # noqa: E402
    ACCOUNTS_FILE,
    BASE_DIR,
    DATA_DIR,
    EXPORTS_DIR,
    TAGS_FILE,
    ensure_layout,
    invoke_cmd,
)
from settings import load_settings, save_settings  # noqa: E402
from state_db import DB  # noqa: E402

POLL_MS = 1000  # период опроса базы
REFRESH_EVERY = 3  # события обновляем реже, чем статусы
LOG_TAIL = 4000  # сколько строк лога держать в окне

TITLE = "VK Feed Parser — массовый запуск"

# Период сбора: подпись в списке и часы (0 — взять из настроек).
PERIODS: list[tuple[str, float]] = [
    ("по настройкам", 0.0),
    ("за 6 часов", 6.0),
    ("за сутки", 24.0),
    ("за 3 суток", 72.0),
    ("за 7 суток", 168.0),
    ("за 30 суток", 720.0),
]

TAG_LABELS = {"pending": "в очереди", "running": "идёт", "finished": "собрано"}
EVENT_LEVELS = {"info": "инфо", "warn": "внимание", "error": "ошибка"}

# Цвета интерфейса и шрифты (шрифты подбираются из установленных в системе).
THEME = {
    "bg": "#eef1f6",
    "card": "#ffffff",
    "border": "#d7dee9",
    "text": "#1d2430",
    "muted": "#6b7a90",
    "accent": "#3478f6",
    "accent_active": "#2b63cf",
    "ok": "#127a58",
    "soft": "#e4e9f2",
    "soft_active": "#d5dceb",
    "row_alt": "#f7f9fc",
    "row_hold": "#fff6e8",
    "row_hold_fg": "#7a4d0d",
    "row_dead": "#fdeceb",
    "row_dead_fg": "#9a2f27",
    "row_run": "#eef3ff",
    "console_bg": "#161c27",
    "console_fg": "#d6dfec",
    "console_dim": "#7d8ca3",
}
FONT_FAMILIES = ("SF Pro Text", "Helvetica Neue", "Segoe UI", "Noto Sans", "DejaVu Sans", "Arial")
MONO_FAMILIES = ("SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono", "Courier New")
FONTS = {"ui": "", "mono": "TkFixedFont"}

# tkinter подключается в main(), чтобы модуль импортировался и без Tk.
tk = None  # type: ignore[assignment]
ttk = None  # type: ignore[assignment]
filedialog = None  # type: ignore[assignment]
messagebox = None  # type: ignore[assignment]

def _resolve_fonts() -> None:
    """Берём первый установленный шрифт из списка кандидатов."""
    try:
        from tkinter import font as tkfont

        installed = {name.lower() for name in tkfont.families()}
    except Exception:
        return
    FONTS["ui"] = next((name for name in FONT_FAMILIES if name.lower() in installed), "")
    FONTS["mono"] = next((name for name in MONO_FAMILIES if name.lower() in installed), "TkFixedFont")


def console_font():
    """Шрифт лога: системный моноширинный или штатный TkFixedFont."""
    mono = FONTS["mono"]
    return "TkFixedFont" if mono == "TkFixedFont" else (mono, 11)


def _style(name: str, **options) -> None:
    """Настройка стиля ttk: молча пропускаем опции, которых нет в теме."""
    try:
        ttk.Style().configure(name, **options)
    except Exception:
        pass


def _style_map(name: str, **options) -> None:
    try:
        ttk.Style().map(name, **options)
    except Exception:
        pass


def configure_style() -> None:
    """Собирает оформление окна: карточки, акцентные кнопки, полосатые таблицы."""
    _resolve_fonts()
    style = ttk.Style()
    for theme in ("clam", "alt", "default"):
        try:
            style.theme_use(theme)
            break
        except Exception:
            continue

    ui = FONTS["ui"]
    _style(".", background=THEME["bg"], foreground=THEME["text"], font=(ui, 11), borderwidth=0)
    _style("TFrame", background=THEME["bg"])
    _style("Card.TFrame", background=THEME["card"])
    _style("TLabel", background=THEME["bg"], foreground=THEME["text"])
    _style("Card.TLabel", background=THEME["card"], foreground=THEME["text"])
    _style("Muted.TLabel", background=THEME["bg"], foreground=THEME["muted"])
    _style("CardMuted.TLabel", background=THEME["card"], foreground=THEME["muted"])
    _style("Title.TLabel", background=THEME["card"], foreground=THEME["text"], font=(ui, 15, "bold"))
    _style("Hint.TLabel", background=THEME["card"], foreground=THEME["muted"], font=(ui, 10))

    _style("TButton", background=THEME["soft"], foreground=THEME["text"], padding=(13, 8))
    _style_map(
        "TButton",
        background=[("active", THEME["soft_active"]), ("disabled", "#eff1f6")],
        foreground=[("disabled", THEME["muted"])],
    )
    _style("Accent.TButton", background=THEME["accent"], foreground="#ffffff", padding=(18, 8))
    _style_map(
        "Accent.TButton",
        background=[("active", THEME["accent_active"]), ("disabled", "#a9c2f6")],
        foreground=[("disabled", "#f2f6ff")],
    )
    _style("Danger.TButton", background="#f6e0dd", foreground=THEME["row_dead_fg"], padding=(13, 8))
    _style_map("Danger.TButton", background=[("active", "#eecfcb")])
    _style("Nav.TButton", background=THEME["card"], foreground=THEME["muted"], padding=(14, 9), anchor="w")
    _style_map(
        "Nav.TButton",
        background=[("active", "#f1f5fc")],
        foreground=[("active", THEME["text"])],
    )
    _style("NavOn.TButton", background=THEME["accent"], foreground="#ffffff", padding=(14, 9), anchor="w")
    _style_map("NavOn.TButton", background=[("active", THEME["accent"])])

    _style("TEntry", fieldbackground=THEME["card"], foreground=THEME["text"], padding=5)
    _style("TSpinbox", fieldbackground=THEME["card"], foreground=THEME["text"], padding=4)
    _style("TCombobox", background=THEME["card"], arrowcolor=THEME["muted"], padding=4)
    _style_map(
        "TCombobox",
        fieldbackground=[("readonly", THEME["card"])],
        foreground=[("readonly", THEME["text"])],
    )
    _style("TCheckbutton", background=THEME["bg"], foreground=THEME["text"])
    _style_map("TCheckbutton", indicatorcolor=[("selected", THEME["accent"]), ("!selected", THEME["card"])])

    _style(
        "Treeview",
        background=THEME["card"],
        fieldbackground=THEME["card"],
        foreground=THEME["text"],
        rowheight=29,
        borderwidth=0,
        font=(ui, 11),
    )
    _style(
        "Treeview.Heading",
        background="#e9eef8",
        foreground=THEME["muted"],
        relief="flat",
        padding=(8, 7),
        font=(ui, 10, "bold"),
    )
    _style_map("Treeview", background=[("selected", THEME["accent"])], foreground=[("selected", "#ffffff")])
    _style_map("Treeview.Heading", background=[("active", "#dee5f4")])
    _style("Vertical.TScrollbar", background="#dde3ee", troughcolor=THEME["bg"], arrowsize=13)
    _style(
        "Bar.Horizontal.TProgressbar",
        troughcolor="#e2e8f3",
        background=THEME["accent"],
        thickness=8,
    )

# Логика без окон: её проверяют тесты, поднимать Tk для этого не нужно
def open_path(path: Path) -> None:
    """Открывает файл или папку системным приложением."""
    path = Path(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        pass


def parse_accounts_text(text: str) -> tuple[list[str], list[str]]:
    """Разбирает вставленные строки аккаунтов: (годные строки, проблемы).

    Комментарии сохраняем как есть, кривые строки не пишем и говорим, что не так.
    """
    keep: list[str] = []
    bad: list[str] = []
    for i, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#") or line.startswith("//"):
            keep.append(line)
            continue
        if parse_account_line(line, i) is None:
            bad.append(f"строка {i}: не похоже на «телефон;пароль;2FA;прокси» — {line}")
        else:
            keep.append(line)
    return keep, bad


def parse_tags_text(text: str) -> tuple[list[str], list[str]]:
    """Теги из строки: «#маникюр, ремонтквартир» → ['маникюр', 'ремонтквартир']."""
    keep: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        raw = chunk.strip()
        if raw.startswith("//") or (raw.startswith("#") and raw[1:2].isspace()):
            continue  # комментарий, а не тег: «// заметка» и «# заметка»
        tag = raw.lstrip("#").strip()
        if not tag:
            continue
        if " " in tag or "\t" in tag:
            bad.append(f"в теге не бывает пробелов: {chunk.strip()}")
            continue
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        keep.append(tag)
    return keep, bad

def tags_in_file(path: Path) -> set[str]:
    """Теги, уже лежащие в файле (в нижнем регистре) — чтобы не писать дубли."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or (line.startswith("#") and line[1:2].isspace()):
            continue  # комментарии как в parse_tags_file: «// заметка» и «# заметка»
        tag = line.lstrip("#").strip()
        if tag:
            out.add(tag.lower())
    return out


def build_run_args(
    *,
    hours: float = 0.0,
    passes: int = 0,
    workers: int = 0,
    validate_first: bool = True,
    full: bool = False,
    no_resume: bool = False,
    dry_run: bool = False,
    tags_file: Optional[Path] = None,
    tag: str = "",
) -> list[str]:
    """Аргументы для run_tags.py — ровно то, что уйдёт в процесс."""
    args: list[str] = []
    if hours:
        args += ["--hours", f"{float(hours):g}"]
    if passes and int(passes) > 1:
        args += ["--passes", str(int(passes))]
    if workers:
        args += ["--workers", str(int(workers))]
    if validate_first:
        args.append("--validate-first")
    if full:
        args.append("--full")
    if no_resume:
        args.append("--no-resume")
    if dry_run:
        args.append("--dry-run")
    if tag:
        args += ["--tag", tag]
    if tags_file:
        args.append(str(tags_file))
    return args


def pool_summary(rows: list[dict], now: Optional[float] = None) -> dict:
    """Считает «работает / отлёжка / мёртв» по строкам таблицы аккаунтов."""
    now = time.time() if now is None else now
    out = {READY: 0, HOLD: 0, DEAD: 0}
    for row in rows:
        status = normalize_status(
            str(row.get("status") or READY), float(row.get("cooldown_until") or 0), now
        )
        out[status] = out.get(status, 0) + 1
    return out


def cooldown_text(row: dict, now: Optional[float] = None) -> str:
    """«осталось 3 мин» для отлёжки — и пусто для остальных статусов."""
    now = time.time() if now is None else now
    left = float(row.get("cooldown_until") or 0) - now
    if left <= 0:
        return ""
    minutes, seconds = divmod(int(left), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"осталось {hours} ч {minutes} мин"
    if minutes:
        return f"осталось {minutes} мин"
    return f"осталось {seconds} с"


def hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""

# Окно
class MassApp:
    """Окно массового запуска: аккаунты, прогон, теги, события и лог."""

    def __init__(self, root) -> None:
        self.root = root
        self.settings = load_settings()
        self.db = DB()
        self.proc: Optional[subprocess.Popen] = None
        self.proc_label = ""
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ticks = 0
        self._acc_key: tuple = ()
        self._tag_key: tuple = ()
        self._evt_key: tuple = ()

        configure_style()
        root.title(TITLE)
        root.geometry("1240x820")
        root.minsize(980, 640)
        root.configure(bg=THEME["bg"])

        self._build_header()
        self._build_cards()
        # Лог крепим к низу окна до тела: так он держит свою высоту,
        # а сжимается под нехватку места область с таблицами.
        self._build_log()
        self._build_body()
        self.show_page("accounts")
        self.refresh_all(force=True)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(POLL_MS, self._tick)

    # шапка
    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=THEME["card"], highlightbackground=THEME["border"], highlightthickness=1)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=THEME["card"], padx=16, pady=12)
        inner.pack(fill="x")

        brand = tk.Frame(inner, bg=THEME["card"])
        brand.pack(side="left")
        tk.Label(
            brand, text="VK Feed Parser", bg=THEME["card"], fg=THEME["text"],
            font=(FONTS["ui"], 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand, text="массовый сбор постов по хештегам", bg=THEME["card"],
            fg=THEME["muted"], font=(FONTS["ui"], 10),
        ).pack(anchor="w")

        actions = tk.Frame(inner, bg=THEME["card"])
        actions.pack(side="right")
        self.state_var = tk.StringVar(value="готов")
        self.state_pill = tk.Label(
            actions, textvariable=self.state_var, bg="#e6f3ec", fg=THEME["ok"],
            padx=12, pady=4, font=(FONTS["ui"], 10, "bold"),
        )
        self.state_pill.pack(side="left", padx=(0, 14))
        ttk.Button(
            actions, text="Обновить", command=lambda: self.refresh_all(force=True)
        ).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Проверить аккаунты", command=self.validate_accounts).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(actions, text="Стоп", style="Danger.TButton", command=self.stop).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(actions, text="Старт", style="Accent.TButton", command=self.start).pack(
            side="right"
        )

    # карточки
    def _build_cards(self) -> None:
        row = tk.Frame(self.root, bg=THEME["bg"])
        row.pack(fill="x", padx=16, pady=12)

        self.stats_var = tk.StringVar(value="читаю базу…")
        self.posts_value = tk.StringVar(value="—")
        self.posts_caption = tk.StringVar(value="")
        self.tags_value = tk.StringVar(value="—")
        self.tags_caption = tk.StringVar(value="собрано / всего")
        self.acc_value = tk.StringVar(value="—")
        self.acc_caption = tk.StringVar(value="")
        self.progress_value = tk.StringVar(value="0%")

        cards = (
            ("Постов в базе", self.posts_value, self.posts_caption),
            ("Теги", self.tags_value, self.tags_caption),
            ("Аккаунты", self.acc_value, self.acc_caption),
            ("Прогресс тегов", self.progress_value, None),
        )
        for column, (caption, value, note) in enumerate(cards):
            row.columnconfigure(column, weight=1, uniform="cards")
            self._card(row, column, caption, value, note)

    def _card(self, parent, column: int, caption: str, value, note) -> None:
        card = tk.Frame(parent, bg=THEME["card"], highlightbackground=THEME["border"], highlightthickness=1)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        box = tk.Frame(card, bg=THEME["card"], padx=14, pady=10)
        box.pack(fill="both", expand=True)
        tk.Label(box, text=caption, bg=THEME["card"], fg=THEME["muted"], font=(FONTS["ui"], 10)).pack(
            anchor="w"
        )
        tk.Label(
            box, textvariable=value, bg=THEME["card"], fg=THEME["text"], font=(FONTS["ui"], 20, "bold")
        ).pack(anchor="w")
        if note is None:
            self.progress = ttk.Progressbar(
                box, mode="determinate", maximum=100, style="Bar.Horizontal.TProgressbar"
            )
            self.progress.pack(fill="x", pady=(8, 0))
        else:
            tk.Label(box, textvariable=note, bg=THEME["card"], fg=THEME["muted"], font=(FONTS["ui"], 10)).pack(
                anchor="w"
            )

    # навигация
    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=THEME["bg"], padx=16)
        body.pack(fill="both", expand=True)

        nav = tk.Frame(
            body, bg=THEME["card"], highlightbackground=THEME["border"], highlightthickness=1, width=190
        )
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        column = tk.Frame(nav, bg=THEME["card"], padx=8, pady=8)
        column.pack(fill="both", expand=True)
        self.nav: dict = {}
        for key, label in (
            ("accounts", "Аккаунты"),
            ("run", "Прогон"),
            ("tags", "Теги"),
            ("events", "События"),
        ):
            button = ttk.Button(
                column, text=label, style="Nav.TButton", command=lambda k=key: self.show_page(k)
            )
            button.pack(fill="x", pady=2)
            self.nav[key] = button

        area = tk.Frame(body, bg=THEME["bg"])
        area.pack(side="left", fill="both", expand=True, padx=(12, 0))
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.pages: dict = {}
        self._build_accounts_page(area)
        self._build_run_page(area)
        self._build_tags_page(area)
        self._build_events_page(area)

    def show_page(self, key: str) -> None:
        """Переключает раздел: кнопку в меню подсвечиваем, страницу поднимаем."""
        for name, button in self.nav.items():
            button.configure(style="NavOn.TButton" if name == key else "Nav.TButton")
        self.pages[key].tkraise()

    @staticmethod
    def _page(parent):
        frame = tk.Frame(
            parent, bg=THEME["card"], highlightbackground=THEME["border"], highlightthickness=1
        )
        frame.grid(row=0, column=0, sticky="nsew")
        return frame

    # аккаунты
    def _build_accounts_page(self, parent) -> None:
        frame = self._page(parent)
        self.pages["accounts"] = frame

        top = tk.Frame(frame, bg=THEME["card"], padx=12, pady=10)
        top.pack(fill="x")
        ttk.Button(top, text="Добавить аккаунты…", command=self.add_accounts_dialog).pack(side="left")
        ttk.Button(
            top, text="Открыть accounts.txt", command=lambda: open_path(ACCOUNTS_FILE)
        ).pack(side="left", padx=6)
        ttk.Button(top, text="Проверить прокси", command=self.check_proxies).pack(side="left")
        ttk.Label(
            top, text="статусы пишет движок: работает / отлёжка / мёртв", style="Hint.TLabel"
        ).pack(side="right")

        cols = ("login", "status", "proxy", "posts", "fails", "cooldown", "error")
        heads = ("Логин", "Статус", "Прокси", "Постов", "Сбоев", "Отлёжка", "Последняя ошибка")
        widths = (230, 100, 190, 80, 70, 130, 360)
        self.acc_tree = self._tree(frame, cols, heads, widths, stretch="error")

    # прогон
    def _build_run_page(self, parent) -> None:
        frame = self._page(parent)
        self.pages["run"] = frame
        box = tk.Frame(frame, bg=THEME["card"], padx=14, pady=12)
        box.pack(fill="both", expand=True)

        form = tk.Frame(box, bg=THEME["card"])
        form.pack(fill="x")
        self.period_var = tk.StringVar(value=PERIODS[0][0])
        ttk.Label(form, text="Период:", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            form,
            textvariable=self.period_var,
            values=[p[0] for p in PERIODS],
            width=16,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=(6, 20))
        self.passes_var = tk.StringVar(value=str(self.settings.get("repeat_passes", 1)))
        ttk.Label(form, text="Проходов:", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(form, from_=1, to=20, textvariable=self.passes_var, width=5).grid(
            row=0, column=3, sticky="w", padx=(6, 20)
        )
        self.workers_var = tk.StringVar(value=str(self.settings.get("max_accounts", 2)))
        ttk.Label(form, text="Потоков:", style="Card.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(form, from_=1, to=64, textvariable=self.workers_var, width=5).grid(
            row=0, column=5, sticky="w", padx=(6, 0)
        )

        self.validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            form, text="проверить аккаунты перед стартом", variable=self.validate_var
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="пройти период заново (--full)", variable=self.full_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(12, 0)
        )
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="примерка (не писать в базу)", variable=self.dry_var).grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(12, 0)
        )

        tags_row = tk.Frame(box, bg=THEME["card"])
        tags_row.pack(fill="x", pady=(14, 0))
        self.tags_file_var = tk.StringVar(value=str(TAGS_FILE))
        ttk.Label(tags_row, text="Файл тегов:", style="Card.TLabel").pack(side="left")
        ttk.Entry(tags_row, textvariable=self.tags_file_var, width=44).pack(side="left", padx=6)
        ttk.Button(tags_row, text="Выбрать…", command=self.pick_tags_file).pack(side="left")
        ttk.Button(
            tags_row, text="Открыть", command=lambda: open_path(Path(self.tags_file_var.get()))
        ).pack(side="left", padx=6)

        add_row = tk.Frame(box, bg=THEME["card"])
        add_row.pack(fill="x", pady=(10, 0))
        self.new_tag_var = tk.StringVar()
        ttk.Label(add_row, text="Добавить тег:", style="Card.TLabel").pack(side="left")
        entry = ttk.Entry(add_row, textvariable=self.new_tag_var, width=30)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda _e: self.add_tags())
        ttk.Button(add_row, text="Добавить", command=self.add_tags).pack(side="left")

        actions = tk.Frame(box, bg=THEME["card"])
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(actions, text="Выгрузить найденное в Excel", command=self.export_excel).pack(
            side="left"
        )
        ttk.Button(
            actions, text="Открыть папку экспорта", command=lambda: open_path(EXPORTS_DIR)
        ).pack(side="left", padx=6)
        ttk.Label(
            box, text="Запуск прогона — кнопка «Старт» в шапке окна.", style="Hint.TLabel"
        ).pack(anchor="w", pady=(16, 0))

    # теги
    def _build_tags_page(self, parent) -> None:
        frame = self._page(parent)
        self.pages["tags"] = frame

        top = tk.Frame(frame, bg=THEME["card"], padx=12, pady=10)
        top.pack(fill="x")
        ttk.Button(top, text="Вернуть тег в очередь", command=self.reset_selected_tag).pack(
            side="left"
        )
        ttk.Button(top, text="Открыть tags.txt", command=lambda: open_path(TAGS_FILE)).pack(
            side="left", padx=6
        )
        ttk.Label(
            top, text="двойной клик по тегу — тоже вернуть в очередь", style="Hint.TLabel"
        ).pack(side="right")

        cols = ("query", "status", "items", "pages", "open", "updated")
        heads = ("Тег", "Состояние", "Постов", "Страниц", "Окон в работе", "Обновлён")
        widths = (240, 120, 90, 90, 140, 110)
        self.tag_tree = self._tree(frame, cols, heads, widths, stretch="query")
        self.tag_tree.bind("<Double-1>", lambda _e: self.reset_selected_tag())

    # события
    def _build_events_page(self, parent) -> None:
        frame = self._page(parent)
        self.pages["events"] = frame

        top = tk.Frame(frame, bg=THEME["card"], padx=12, pady=10)
        top.pack(fill="x")
        ttk.Label(
            top, text="капчи, ошибки токена и пустые выдачи из базы", style="Hint.TLabel"
        ).pack(side="left")

        cols = ("time", "level", "kind", "account", "tag", "message")
        heads = ("Время", "Уровень", "Тип", "Аккаунт", "Тег", "Сообщение")
        widths = (90, 90, 150, 170, 140, 460)
        self.evt_tree = self._tree(frame, cols, heads, widths, stretch="message")

    @staticmethod
    def _tree(parent, cols: tuple, heads: tuple, widths: tuple, stretch: str):
        """Таблица со скроллбаром — одна и та же на всех страницах."""
        wrap = tk.Frame(parent, bg=THEME["card"])
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        tree = ttk.Treeview(wrap, columns=cols, show="headings", height=10)
        for col, head, width in zip(cols, heads, widths):
            tree.heading(col, text=head)
            tree.column(col, width=width, minwidth=60, anchor="w", stretch=(col == stretch))
        bar = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        tree.tag_configure("odd", background=THEME["row_alt"])
        tree.tag_configure("even", background=THEME["card"])
        tree.tag_configure("hold", background=THEME["row_hold"], foreground=THEME["row_hold_fg"])
        tree.tag_configure("dead", background=THEME["row_dead"], foreground=THEME["row_dead_fg"])
        tree.tag_configure("run", background=THEME["row_run"], foreground=THEME["accent_active"])
        return tree

    @staticmethod
    def _row_tag(status: str, position: int) -> str:
        """Подсветка строки: отлёжка и «мёртв» заметнее обычной зебры."""
        if status == HOLD:
            return "hold"
        if status == DEAD:
            return "dead"
        return "odd" if position % 2 else "even"

    # панель лога
    def _build_log(self) -> None:
        wrap = tk.Frame(self.root, bg=THEME["bg"])
        wrap.pack(side="bottom", fill="x", padx=16, pady=(10, 14))
        card = tk.Frame(wrap, bg=THEME["console_bg"], highlightbackground=THEME["border"], highlightthickness=1)
        card.pack(fill="both", expand=True)

        head = tk.Frame(card, bg=THEME["console_bg"], padx=12, pady=8)
        head.pack(fill="x")
        tk.Label(
            head, text="Лог процесса", bg=THEME["console_bg"], fg=THEME["console_fg"],
            font=(FONTS["ui"], 10, "bold"),
        ).pack(side="left")
        self.log_state = tk.Label(
            head, text="ожидание", bg=THEME["console_bg"], fg=THEME["console_dim"],
            font=(FONTS["ui"], 10),
        )
        self.log_state.pack(side="right")
        ttk.Button(head, text="Очистить", command=self.clear_log).pack(side="right", padx=(0, 10))

        body = tk.Frame(card, bg=THEME["console_bg"])
        body.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            body,
            height=10,
            wrap="none",
            font=console_font(),
            bg=THEME["console_bg"],
            fg=THEME["console_fg"],
            insertbackground=THEME["console_fg"],
            selectbackground=THEME["accent"],
            relief="flat",
            padx=12,
            pady=6,
            state="disabled",
        )
        bar = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=bar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # чтение состояния
    def refresh_all(self, force: bool = False) -> None:
        try:
            rows = self.db.list_accounts()
        except Exception as exc:  # noqa: BLE001
            self.log(f"база недоступна: {exc}")
            return

        now = time.time()
        summary = pool_summary(rows)
        acc_key = tuple(
            (
                r.get("login"),
                r.get("status"),
                r.get("total_items"),
                r.get("fail_count"),
                int(float(r.get("cooldown_until") or 0)),
                (r.get("last_error") or "")[:60],
            )
            for r in rows
        )
        if force or acc_key != self._acc_key:
            self._acc_key = acc_key
            self.acc_tree.delete(*self.acc_tree.get_children())
            for position, row in enumerate(rows):
                status = normalize_status(
                    str(row.get("status") or READY), float(row.get("cooldown_until") or 0), now
                )
                item = self.acc_tree.insert(
                    "",
                    "end",
                    values=(
                        row.get("login"),
                        status_label(status),
                        row.get("proxy") or "—",
                        int(row.get("total_items") or 0),
                        int(row.get("fail_count") or 0),
                        cooldown_text(row, now) or "—",
                        (row.get("last_error") or "").strip() or "—",
                    ),
                )
                self.acc_tree.item(item, tags=(self._row_tag(status, position),))

        stats = self.db.stats()
        posts = int(stats["posts"])
        finished = int(stats["tags_finished"])
        total = int(stats["tags_total"])
        self.stats_var.set(
            f"постов {posts} · теги {finished}/{total} · аккаунты: работает {summary[READY]}, "
            f"отлёжка {summary[HOLD]}, мёртв {summary[DEAD]}"
        )
        self.posts_value.set(str(posts))
        self.posts_caption.set(f"+{int(stats['posts_last_hour'])} за последний час")
        self.tags_value.set(f"{finished} / {total}")
        self.acc_value.set(str(len(rows)))
        self.acc_caption.set(
            f"работает {summary[READY]} · отлёжка {summary[HOLD]} · мёртв {summary[DEAD]}"
        )
        percent = (100.0 * finished / total) if total else 0.0
        self.progress["value"] = percent
        self.progress_value.set(f"{percent:.0f}%")

        self._refresh_tags(force)
        if force or self.ticks % REFRESH_EVERY == 0:
            self._refresh_events(force)

    def _refresh_tags(self, force: bool = False) -> None:
        rows = self.db.all_tags()
        key = tuple(
            (
                r.get("query"),
                r.get("status"),
                r.get("total_items"),
                r.get("total_pages"),
                int(float(r.get("updated_at") or 0)),
            )
            for r in rows
        )
        if not force and key == self._tag_key:
            return
        self._tag_key = key
        self.tag_tree.delete(*self.tag_tree.get_children())
        for position, row in enumerate(rows):
            status = str(row.get("status") or "")
            item = self.tag_tree.insert(
                "",
                "end",
                values=(
                    row.get("query"),
                    TAG_LABELS.get(status, status),
                    int(row.get("total_items") or 0),
                    int(row.get("total_pages") or 0),
                    self.db.count_open_jobs(str(row.get("query"))),
                    hhmmss(float(row.get("updated_at") or 0)),
                ),
            )
            if status == "running":
                self.tag_tree.item(item, tags=("run",))
            else:
                self.tag_tree.item(item, tags=("odd" if position % 2 else "even",))

    def _refresh_events(self, force: bool = False) -> None:
        events = self.db.recent_events(40)
        key = tuple(e.get("id") for e in events)
        if not force and key == self._evt_key:
            return
        self._evt_key = key
        self.evt_tree.delete(*self.evt_tree.get_children())
        for position, event in enumerate(events):
            level = str(event.get("level") or "")
            item = self.evt_tree.insert(
                "",
                "end",
                values=(
                    hhmmss(float(event.get("created_at") or 0)),
                    EVENT_LEVELS.get(level, level),
                    event.get("kind") or "",
                    event.get("account") or "",
                    event.get("tag") or "",
                    event.get("message") or "",
                ),
            )
            # ошибки и предупреждения подсвечиваем как в таблице аккаунтов
            tag = {"error": "dead", "warn": "hold"}.get(level)
            self.evt_tree.item(item, tags=(tag or ("odd" if position % 2 else "even"),))

    def _tick(self) -> None:
        self.ticks += 1
        self._drain_log()
        self.refresh_all()
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            label = self.proc_label
            self.proc = None
            self._set_running(False)
            self.log(f"{label}: процесс завершён (код {code})")
        self.root.after(POLL_MS, self._tick)

    # запись в лог
    def log(self, msg: str) -> None:
        text = self.log_text
        text.configure(state="normal")
        text.insert("end", msg + "\n")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > LOG_TAIL:
            text.delete("1.0", f"{lines - LOG_TAIL}.0")
        text.see("end")
        text.configure(state="disabled")

    def _drain_log(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                return
            self.log(line)

    def _reader(self, proc: subprocess.Popen) -> None:
        """Читает вывод процесса построчно в очередь: UI обновляет главный поток."""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log_queue.put(line.rstrip("\n"))
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(f"чтение вывода прервалось: {exc}")

    # запуск процессов
    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        kwargs: dict = {
            "cwd": str(BASE_DIR),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":  # чтобы «Стоп» умел послать Ctrl+Break
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(cmd, **kwargs)

    def _run_cli(self, script: str, extra: list[str], label: str) -> bool:
        """Запускает модуль из src/ отдельным процессом и льёт его вывод в лог."""
        cmd = invoke_cmd(script) + list(extra)
        self.log(f"$ {' '.join(cmd)}")
        return self._spawn(cmd, label)

    def _spawn(self, cmd: list[str], label: str) -> bool:
        """Общий запуск: один процесс за раз, вывод в лог, состояние в шапке."""
        if self.proc is not None:
            self.log("уже идёт другой процесс — дождитесь конца или нажмите «Стоп»")
            return False
        try:
            proc = self._popen(cmd)
        except Exception as exc:  # noqa: BLE001
            self.log(f"не удалось запустить {label}: {exc}")
            return False
        self.proc = proc
        self.proc_label = label
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        self._set_running(True)
        return True

    def _set_running(self, running: bool) -> None:
        if running:
            self.state_var.set(f"идёт: {self.proc_label}…")
            self.state_pill.configure(bg="#e4edff", fg=THEME["accent"])
            self.log_state.configure(text="процесс запущен", fg=THEME["console_fg"])
        else:
            self.state_var.set("готов")
            self.state_pill.configure(bg="#e6f3ec", fg=THEME["ok"])
            self.log_state.configure(text="ожидание", fg=THEME["console_dim"])

    def start(self) -> None:
        workers = self._int(self.workers_var, self.settings.get("max_accounts", 2))
        passes = self._int(self.passes_var, 1)
        hours = dict(PERIODS).get(self.period_var.get(), 0.0)
        tags_file = Path(self.tags_file_var.get().strip() or TAGS_FILE)

        self.settings["max_accounts"] = max(1, workers)
        self.settings["repeat_passes"] = max(1, passes)
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.log(f"настройки не сохранились: {exc}")

        args = build_run_args(
            hours=hours,
            passes=passes,
            workers=workers,
            validate_first=bool(self.validate_var.get()),
            full=bool(self.full_var.get()),
            dry_run=bool(self.dry_var.get()),
            tags_file=tags_file,
        )
        self._run_cli("run_tags", args, "прогон")

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            self.log("нечего останавливать: процесс не запущен")
            return
        self.log(f"останавливаю {self.proc_label} — прогресс сохраняется…")
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception as exc:  # noqa: BLE001
            self.log(f"не получилось послать Ctrl+C ({exc}) — завершаю процесс")
            proc.terminate()
        self.root.after(15000, self._force_kill)

    def _force_kill(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self.log("процесс не ответил на Ctrl+C — завершаю принудительно")
            proc.terminate()

    def validate_accounts(self) -> None:
        workers = self._int(self.workers_var, self.settings.get("max_accounts", 2))
        self._run_cli(
            "account_pool", ["--validate", "--workers", str(workers)], "проверка аккаунтов"
        )

    def check_proxies(self) -> None:
        """Проверка прокси из proxies.txt: живы ли и пускают ли на VK."""
        self._run_cli("check_proxy", [], "проверка прокси")

    def export_excel(self) -> None:
        self._run_cli("export_xlsx", [], "выгрузка в Excel")

    def reset_selected_tag(self) -> None:
        """Возвращает выбранный тег в очередь: следующий старт соберёт его заново."""
        selection = self.tag_tree.selection()
        if not selection:
            messagebox.showinfo("Теги", "выберите тег в таблице")
            return
        tag = str(self.tag_tree.item(selection[0], "values")[0])
        if not messagebox.askyesno(
            "Теги",
            f"Вернуть тег «{tag}» в очередь?\n\n"
            "Курсоры и состояние тега сбросятся, собранные посты останутся в базе —\n"
            "следующий старт пройдёт период заново.",
        ):
            return
        try:
            count = self.db.reset_tags([tag])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Теги", f"не получилось сбросить «{tag}»: {exc}")
            return
        self.log(f"тег «{tag}» вернулся в очередь (сброшено записей: {count})")
        self.refresh_all(force=True)

    # диалоги
    def pick_tags_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Файл со списком тегов",
            initialdir=str(DATA_DIR),
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
        )
        if path:
            self.tags_file_var.set(path)

    def add_tags(self) -> None:
        tags, bad = parse_tags_text(self.new_tag_var.get())
        if bad:
            messagebox.showwarning("Теги", "\n".join(bad))
        if not tags:
            return
        path = Path(self.tags_file_var.get().strip() or TAGS_FILE)
        try:
            already = tags_in_file(path)
        except OSError as exc:
            messagebox.showerror("Теги", f"не удалось прочитать {path}: {exc}")
            return
        fresh = [tag for tag in tags if tag.lower() not in already]
        if not fresh:
            self.new_tag_var.set("")
            self.log(f"{path.name}: теги уже в файле — {', '.join(tags)}")
            return
        try:
            with path.open("a", encoding="utf-8") as fh:
                for tag in fresh:
                    fh.write(tag + "\n")
        except OSError as exc:
            messagebox.showerror("Теги", f"не удалось записать {path}: {exc}")
            return
        self.new_tag_var.set("")
        self.log(f"{path.name}: добавлено тегов — {', '.join(fresh)}")
        if len(fresh) < len(tags):
            skipped = ", ".join(tag for tag in tags if tag not in fresh)
            self.log(f"{path.name}: уже было — {skipped}")
        self.refresh_all(force=True)

    def add_accounts_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Добавить аккаунты")
        win.geometry("680x420")
        win.configure(bg=THEME["card"])

        head = tk.Frame(win, bg=THEME["card"], padx=14, pady=12)
        head.pack(fill="x")
        tk.Label(
            head,
            text="По одной строке на аккаунт:  телефон;пароль;2FA;прокси\n"
            "2FA и прокси необязательны; пустой прокси — берётся из proxies.txt",
            justify="left",
            bg=THEME["card"],
            fg=THEME["text"],
            font=(FONTS["ui"], 11),
        ).pack(anchor="w")

        box = tk.Frame(win, bg=THEME["card"], padx=14)
        box.pack(fill="both", expand=True)
        text = tk.Text(
            box,
            height=12,
            wrap="none",
            font=console_font(),
            relief="flat",
            bg=THEME["row_alt"],
            fg=THEME["text"],
            padx=10,
            pady=8,
        )
        text.pack(fill="both", expand=True)

        def save() -> None:
            keep, bad = parse_accounts_text(text.get("1.0", "end"))
            if bad:
                messagebox.showwarning("Аккаунты", "\n".join(bad[:10]))
            if not keep:
                return
            try:
                with ACCOUNTS_FILE.open("a", encoding="utf-8") as fh:
                    fh.write("\n" + "\n".join(keep) + "\n")
            except OSError as exc:
                messagebox.showerror("Аккаунты", f"не удалось записать {ACCOUNTS_FILE}: {exc}")
                return
            loaded = load_accounts()
            self.log(
                f"{ACCOUNTS_FILE.name}: дописано строк {len(keep)}, "
                f"всего аккаунтов в файле {len(loaded)}"
            )
            self.refresh_all(force=True)
            win.destroy()

        bar = tk.Frame(win, bg=THEME["card"], padx=14, pady=12)
        bar.pack(fill="x")
        tk.Label(
            bar,
            text=f"строки допишутся в конец {ACCOUNTS_FILE.name}",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=(FONTS["ui"], 10),
        ).pack(side="left")
        ttk.Button(bar, text="Добавить в файл", style="Accent.TButton", command=save).pack(
            side="right"
        )
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="right", padx=6)

    @staticmethod
    def _int(var, default: int) -> int:
        try:
            return int(str(var.get()).strip())
        except (ValueError, AttributeError):
            return int(default)

    def on_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno("Выход", "Прогон ещё идёт. Остановить и выйти?"):
                return
            self.stop()
        self.root.destroy()


# Точка входа
def launcher_hint() -> str:
    """Что доустановить, если в Python нет Tk."""
    if sys.platform == "darwin":
        return "brew install python-tk"
    if sys.platform.startswith("win"):
        return "переустановите Python с галочкой «tcl/tk and IDLE»"
    return "sudo apt install python3-tk"


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    ensure_layout()
    try:
        import tkinter as tk_mod
        from tkinter import filedialog as fd_mod
        from tkinter import messagebox as mb_mod
        from tkinter import ttk as ttk_mod
    except ImportError:
        print("В этом Python нет tkinter — окно массового запуска недоступно.")
        print(f"Поставьте Tk: {launcher_hint()}")
        print("Пока можно работать из консоли: python src/run_tags.py --help")
        return 3

    global tk, ttk, filedialog, messagebox
    tk, ttk, filedialog, messagebox = tk_mod, ttk_mod, fd_mod, mb_mod

    root = tk.Tk()
    if "--selftest" in argv:
        root.withdraw()  # окно скрыто: проверяем сборку UI, базу и таблицы
        app = MassApp(root)
        app._tick()
        print(f"окно собрано: {app.stats_var.get()}")
        print(f"аккаунтов в таблице: {len(app.acc_tree.get_children())}")
        print(f"тегов в таблице: {len(app.tag_tree.get_children())}")
        print(f"событий в таблице: {len(app.evt_tree.get_children())}")
        root.destroy()
        return 0

    app = MassApp(root)
    app.log(f"окно массового запуска · база {DATA_DIR / 'parser.sqlite3'}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
