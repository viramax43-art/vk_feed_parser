#!/usr/bin/env python3
"""Выгрузка найденных постов в Excel (xlsxwriter).

Запуск:
    python src/export_xlsx.py                       # все посты → data/exports/
    python src/export_xlsx.py --tag маникюр
    python src/export_xlsx.py --hours 24 --out data/exports/fresh.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import EXPORTS_DIR  # noqa: E402
from state_db import DB  # noqa: E402

COLUMNS = [
    ("tag", "Теги", 22),
    ("date_str", "Дата", 20),
    ("author_name", "Автор", 26),
    ("author_kind", "Тип", 8),
    ("author_url", "Страница автора", 30),
    ("text", "Текст", 70),
    ("likes", "Лайки", 9),
    ("comments", "Комментарии", 12),
    ("reposts", "Репосты", 9),
    ("views", "Просмотры", 11),
    ("photos", "Фото", 7),
    ("videos", "Видео", 7),
    ("docs", "Док", 6),
    ("attachments", "Вложения", 22),
    ("url", "Ссылка на пост", 34),
]


def export_posts(
    db: DB,
    path: Optional[Path] = None,
    *,
    tag: Optional[str] = None,
    since: Optional[float] = None,
    limit: Optional[int] = None,
) -> Path:
    """Пишет xlsx со всеми постами (+ листы «Теги» и «Аккаунты»)."""
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("нужен пакет xlsxwriter: pip install xlsxwriter") from exc

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if path is None:
        suffix = f"_{tag}" if tag else ""
        path = EXPORTS_DIR / f"vk_feed{suffix}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = db.export_rows(tag=tag, since=since, limit=limit)
    workbook = xlsxwriter.Workbook(str(path), {"constant_memory": False})
    head = workbook.add_format({"bold": True, "bg_color": "#EDE7F6", "border": 1, "valign": "top"})
    wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
    plain = workbook.add_format({"valign": "top"})
    link = workbook.add_format({"font_color": "#0645AD", "underline": 1, "valign": "top"})

    sheet = workbook.add_worksheet("Посты")
    for col, (_, title, width) in enumerate(COLUMNS):
        sheet.set_column(col, col, width)
        sheet.write(0, col, title, head)
    sheet.freeze_panes(1, 0)

    for idx, row in enumerate(rows, start=1):
        for col, (key, _, _) in enumerate(COLUMNS):
            value = row.get(key, "")
            if key == "url" and value:
                sheet.write_url(idx, col, str(value), link, str(value))
            elif key in ("text", "attachments"):
                sheet.write(idx, col, str(value or ""), wrap)
            else:
                sheet.write(idx, col, value if value is not None else "", plain)
    if rows:
        sheet.autofilter(0, 0, len(rows), len(COLUMNS) - 1)

    tags_sheet = workbook.add_worksheet("Теги")
    tag_head = ["Тег", "Статус", "Постов", "Страниц", "Проходов", "Курсор", "Причина", "Обновлён"]
    for col, title in enumerate(tag_head):
        tags_sheet.write(0, col, title, head)
    tags_sheet.set_column(0, 0, 22)
    tags_sheet.set_column(1, 1, 12)
    tags_sheet.set_column(5, 5, 26)
    tags_sheet.set_column(6, 6, 18)
    for idx, state in enumerate(db.all_tags(), start=1):
        tags_sheet.write(idx, 0, state.get("query") or "")
        tags_sheet.write(idx, 1, state.get("status") or "")
        tags_sheet.write(idx, 2, int(state.get("total_items") or 0))
        tags_sheet.write(idx, 3, int(state.get("total_pages") or 0))
        tags_sheet.write(idx, 4, int(state.get("passes_done") or 0))
        tags_sheet.write(idx, 5, str(state.get("next_from") or ""))
        tags_sheet.write(idx, 6, state.get("stop_reason") or "")
        updated = float(state.get("updated_at") or 0)
        tags_sheet.write(
            idx, 7, time.strftime("%Y-%m-%d %H:%M", time.localtime(updated)) if updated else ""
        )

    acc_sheet = workbook.add_worksheet("Аккаунты")
    acc_head = ["Логин", "user_id", "Прокси", "Статус", "Постов", "Ошибка", "Обновлён"]
    for col, title in enumerate(acc_head):
        acc_sheet.write(0, col, title, head)
    acc_sheet.set_column(0, 0, 18)
    acc_sheet.set_column(2, 2, 34)
    acc_sheet.set_column(5, 5, 30)
    accounts = db.con.execute("SELECT * FROM accounts ORDER BY login").fetchall()
    for idx, acc in enumerate(accounts, start=1):
        acc_sheet.write(idx, 0, acc["login"])
        acc_sheet.write(idx, 1, int(acc["user_id"] or 0))
        acc_sheet.write(idx, 2, acc["proxy"] or "")
        acc_sheet.write(idx, 3, acc["status"] or "")
        acc_sheet.write(idx, 4, int(acc["total_items"] or 0))
        acc_sheet.write(idx, 5, acc["last_error"] or "")
        updated = float(acc["updated_at"] or 0)
        acc_sheet.write(
            idx, 6, time.strftime("%Y-%m-%d %H:%M", time.localtime(updated)) if updated else ""
        )

    workbook.close()
    print(f"выгружено постов: {len(rows)} (один пост = одна строка) → {path}")
    return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Выгрузка постов VK в Excel")
    parser.add_argument("--tag", default="")
    parser.add_argument("--hours", type=float, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    db = DB()
    since = time.time() - args.hours * 3600 if args.hours else None
    path = export_posts(
        db,
        Path(args.out) if args.out else None,
        tag=args.tag or None,
        since=since,
        limit=args.limit or None,
    )
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    print(f"файл: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
