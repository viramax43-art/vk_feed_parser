#!/usr/bin/env python3
"""Проба одного тега: newsfeed.search + запись в SQLite.

Запуск:
    python src/probe.py маникюр --pages 5 --count 50
    python src/probe.py маникюр --hours 6                       # только свежие
    python src/probe.py маникюр --since 2026-09-01 --until 2026-09-28
    python src/probe.py маникюр --passes 3 --repeat-pause 30    # повторные проходы
    python src/probe.py маникюр --json                          # сырой ответ VK
    python src/probe.py маникюр --dry-run                       # без записи в базу
    python src/probe.py маникюр --tag-query word                # искать словом, без #

Чем искать тег — хештегом (#тег) или словом без решётки — режим tag_query из
data/settings.json (страница «Теги» в панели); флаг --tag-query перебивает настройку.
В режиме «авто» проба один раз спрашивает обе выдачи за сутки и берёт ту, где постов
больше (в прогоне так же — по каждому тегу отдельно, см. run_tags.tag_mode_for).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import load_accounts, normalize_proxy, resolve_proxy  # noqa: E402
from paths import LOG_DIR, SESSION_FILE  # noqa: E402
from settings import TAG_QUERY_LABELS, load_settings  # noqa: E402
from state_db import DB  # noqa: E402
from vk_client import (  # noqa: E402
    MAX_RESULTS,
    MODE_PROBE_HOURS,
    VkApiError,
    VkAuthError,
    VkCaptchaError,
    VkFeedClient,
    VkTokenError,
    detect_tag_mode,
    extract_tag,
    set_tag_query,
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_time(value: Optional[str]) -> Optional[int]:
    """Принимает unix-время, YYYY-MM-DD или YYYY-MM-DD HH:MM[:SS]."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"не понимаю время: {value!r}")


def build_client(args) -> VkFeedClient:
    """Клиент из --token / --session / аккаунта из accounts.txt."""
    pause = float(args.pause or 0.0)
    if args.token:
        return VkFeedClient(args.token, args.proxy or "", pause=pause)
    session_path = Path(args.session)
    proxy = normalize_proxy(args.proxy) if args.proxy else None
    if not session_path.exists() and args.account:
        accounts = load_accounts()
        if not accounts:
            raise SystemExit("нет аккаунтов в data/accounts.txt")
        idx = max(1, args.account) - 1
        acc = accounts[min(idx, len(accounts) - 1)]
        session_path = acc.session_path
        if proxy is None:
            proxy = resolve_proxy(acc)  # у аккаунта свой прокси (или direct)
    return VkFeedClient.from_session(session_path, proxy, pause=pause)


def run_tag(
    client: VkFeedClient,
    db: Optional[DB],
    tag: str,
    *,
    pages: int,
    count: int,
    passes: int,
    repeat_pause: int,
    start_time: Optional[int],
    end_time: Optional[int],
    resume: bool,
    dry_run: bool,
    stop_when_no_new: int,
    json_log: bool = False,
) -> dict:
    """Гонит тег по страницам next_from (с повторами) и пишет посты в базу."""
    tag = extract_tag(tag)
    summary = {
        "tag": tag,
        "pages": 0,
        "seen": 0,
        "new": 0,
        "passes": 0,
        "status": "finished",
        "reason": "end_of_feed",
        "cursor": "",
        "dupes": 0,
        "junk": 0,
    }
    cursor = ""
    if resume and db is not None:
        cursor = str(db.tag_state(tag).get("next_from") or "")
        if cursor:
            log(f"#{tag}: продолжаю с курсора {cursor[:24]!r}")

    for pass_no in range(1, max(1, passes) + 1):
        summary["passes"] = pass_no
        page_no = 0
        empty_streak = 0
        ended = False
        while page_no < max(1, pages):
            page_no += 1
            try:
                result = client.search_hashtag(
                    tag,
                    count=count,
                    start_from=cursor or None,
                    start_time=start_time,
                    end_time=end_time,
                )
            except VkTokenError as exc:
                log(f"#{tag}: токен недействителен ({exc}) — нужен повторный вход")
                summary.update(status="error", reason="token")
                return summary
            except VkCaptchaError as exc:
                log(f"#{tag}: VK требует капчу ({exc})")
                summary.update(status="error", reason="captcha")
                return summary
            except VkApiError as exc:
                if exc.code == 100 and cursor:
                    log(f"#{tag}: VK отверг курсор ({exc.message}) — начинаю с начала")
                    cursor = ""
                    page_no -= 1
                    continue
                log(f"#{tag}: ошибка API ({exc})")
                summary.update(status="error", reason=f"api_{exc.code}")
                return summary
            except Exception as exc:  # noqa: BLE001
                log(f"#{tag}: сеть/иное ({type(exc).__name__}: {exc})")
                summary.update(status="error", reason="network")
                return summary

            items = result["items"]
            summary["pages"] += 1
            summary["seen"] += len(items)
            if items and not dry_run and db is not None:
                fresh = db.add_posts(items, tag, pass_no=pass_no, page=page_no, stats=summary)
            else:
                fresh = len(items)
            summary["new"] += max(0, fresh)
            empty_streak = empty_streak + 1 if fresh <= 0 else 0

            log(
                f"#{tag} проход {pass_no}/{passes} стр.{page_no}: "
                f"постов {len(items)}, новых {fresh}, у VK всего {result['total']}"
                + ("" if items else " (пусто)")
            )
            if json_log and page_no == 1:
                dump = LOG_DIR / f"probe_{tag}_p{page_no}.json"
                dump.parent.mkdir(parents=True, exist_ok=True)
                dump.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log(f"сырой ответ: {dump}")

            cursor = result.get("next_from") or ""
            if db is not None and not dry_run:
                db.bump_tag(tag, items=len(items), pages=1, next_from=cursor, pass_no=pass_no)

            if not cursor:
                summary["reason"] = "end_of_feed"
                ended = True
                break
            if empty_streak >= max(1, stop_when_no_new):
                summary["reason"] = "no_new_posts"
                ended = True
                break

        if ended:
            break
        if pass_no < max(1, passes):
            summary["reason"] = "page_limit"
            if repeat_pause:
                log(f"#{tag}: проход {pass_no} закончен (лимит страниц), пауза {repeat_pause} с")
                time.sleep(repeat_pause)
            cursor = ""

    summary["cursor"] = cursor
    if db is not None and not dry_run:
        db.finish_tag(tag, status="partial" if cursor else "finished", reason=summary["reason"])
        db.event(
            "info",
            "probe",
            f"#{tag}: новых {summary['new']}, страниц {summary['pages']}",
            tag=tag,
        )
    return summary


def run_window(
    client: VkFeedClient,
    db: Optional[DB],
    tag: str,
    *,
    start_time: int,
    end_time: int,
    count: int,
    max_pages: int,
    cursor: str = "",
    stop_when_no_new: int = 2,
    dry_run: bool = False,
    on_page: Optional[callable] = None,
    round_no: int = 1,
    seen_before: int = 0,
    split_if_seen: int = 30,
    mode: Optional[str] = None,
) -> dict:
    """Листает одно окно [start_time, end_time) тега до конца ленты.

    `mode` — чем искать тег в этом окне ("hash"/"word"; None — общий режим прогона):
    авто-режим передаёт сюда выбор по каждому тегу (см. run_tags.tag_mode_for).

    Возвращает сводку; need_split=True — VK отдал меньше, чем нашёл (упёрлись в
    лимит выдачи) или кончились страницы: окно надо дробить. on_page(cursor,
    seen, new) вызывается после каждой страницы, чтобы сохранить курсор и
    продолжить окно после падения.

    Глубже первых MAX_RESULTS (1000) записей newsfeed.search не отдаёт: страницы
    начинают повторяться или приходят пустыми. Поэтому окно, дошедшее до 1000,
    сразу помечается need_split — по времени его добьют половинки, а не
    потерянный остаток (см. run_tags.split_job).
    """
    tag = extract_tag(tag)
    out = {
        "status": "ok", "reason": "end_of_feed", "pages": 0, "seen": 0,
        "new": 0, "total": 0, "cursor": cursor or "", "need_split": False,
        "oldest": 0,  # дата самого старого полученного поста — граница остатка окна
        "dupes": 0, "junk": 0,  # сколько постов не записали (повторы/мусор)
    }
    empty_streak = 0
    while out["pages"] < max(1, max_pages):
        try:
            result = client.search_hashtag(
                tag, count=count, start_from=out["cursor"] or None,
                start_time=start_time, end_time=end_time, mode=mode,
            )
        except VkTokenError:
            return {**out, "status": "error", "reason": "token"}
        except VkCaptchaError:
            return {**out, "status": "error", "reason": "captcha"}
        except VkApiError as exc:
            if exc.code == 100 and out["cursor"]:
                out["cursor"] = ""  # VK отверг старый курсор — окно с начала
                continue
            return {**out, "status": "error", "reason": f"api_{exc.code}"}
        except Exception as exc:  # noqa: BLE001
            return {**out, "status": "error", "reason": f"network: {type(exc).__name__}"}

        items = result["items"]
        out["pages"] += 1
        out["seen"] += len(items)
        if out["pages"] == 1:
            out["total"] = int(result.get("total") or 0)
        fresh = 0
        if items and db is not None and not dry_run:
            fresh = db.add_posts(items, tag, pass_no=round_no, page=out["pages"], stats=out)
            db.bump_tag(tag, items=len(items), pages=1)
        elif items:
            fresh = len(items)
        out["new"] += fresh
        dates = [int(p.get("date") or 0) for p in items if p.get("date")]
        if dates:
            out["oldest"] = min(dates) if not out["oldest"] else min(out["oldest"], *dates)
        out["cursor"] = result.get("next_from") or ""
        if on_page:
            on_page(out["cursor"], len(items), fresh)

        # VK не отдаёт глубже первых 1000 результатов (даже с offset/start_from):
        # следующие страницы повторяют уже отданное или приходят пустыми. Дальше
        # листать бессмысленно — окно делим по времени, половинки доберут остаток.
        if out["seen"] >= MAX_RESULTS:
            out["reason"] = "capped"
            out["need_split"] = True
            return out

        if not out["cursor"]:
            break
        empty_streak = empty_streak + 1 if fresh <= 0 else 0
        if empty_streak >= max(1, stop_when_no_new):
            out["reason"] = "no_new_posts"  # дальше уже собранное
            return out
    else:
        out["reason"] = "page_limit"
        out["need_split"] = True
        return out

    # Лента окна кончилась, а VK говорит, что нашёл больше — выдача обрезана
    # лимитом, добираем остаток окна. Если total завышен, остаток вернёт
    # пустую страницу за 1 запрос и закроется.
    # ВК отдаёт ~60 постов на запрос и при этом может «сказать», что больше нет.
    # Поэтому крупную выдачу всегда проверяем остатком ниже самого старого поста
    # (пустой остаток = 1 дешёвый запрос).
    # «Всего» у ВК ненадёжно (почти всегда отданное + 1), поэтому делим
    # только по факту: окно вернуло много постов — значит могло быть обрезано.
    if out["seen"] >= split_if_seen:
        out["need_split"] = True
        out["reason"] = "capped"
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Проба одного хештега VK")
    parser.add_argument("tag", help="хештег, решётка необязательна")
    parser.add_argument("--pages", type=int, default=0, help="страниц за проход")
    parser.add_argument("--count", type=int, default=0, help="постов на страницу (10..200)")
    parser.add_argument("--passes", type=int, default=0, help="повторных проходов")
    parser.add_argument("--repeat-pause", type=int, default=0)
    parser.add_argument("--hours", type=float, default=0, help="только за последние N часов")
    parser.add_argument("--days", type=float, default=0, help="только за последние N суток")
    parser.add_argument("--since", default="", help="с какого времени (unix/YYYY-MM-DD[ HH:MM])")
    parser.add_argument("--until", default="", help="по какое время")
    parser.add_argument("--session", default=str(SESSION_FILE))
    parser.add_argument("--account", type=int, default=0, help="номер аккаунта из accounts.txt")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument(
        "--tag-query",
        choices=("hash", "word", "auto"),
        default=None,
        help="чем искать тег: hash — с решёткой (#тег, как было), word — словом без "
             "решётки (посты, где тег встречается в тексте), auto — выбрать по тегу "
             "пробными запросами. По умолчанию — из data/settings.json (tag_query, "
             "страница «Теги»)",
    )
    parser.add_argument("--no-resume", action="store_true", help="игнорировать курсор из базы")
    parser.add_argument("--dry-run", action="store_true", help="не писать в базу")
    parser.add_argument("--json", action="store_true", help="сохранить сырой ответ первой страницы")
    args = parser.parse_args(argv)

    settings = load_settings()
    pages = args.pages or int(settings["max_pages_per_tag"])
    count = args.count or int(settings["page_count"])
    passes = args.passes or int(settings["repeat_passes"])
    repeat_pause = args.repeat_pause or int(settings["repeat_pause_sec"])
    stop_when_no_new = int(settings["stop_when_no_new_pages"])
    pause = args.pause or float(settings["request_pause_sec"])
    args.pause = pause

    start_time = parse_time(args.since)
    end_time = parse_time(args.until)
    if args.hours or args.days:
        start_time = int(time.time() - (args.hours * 3600 + args.days * 86400))
    if not start_time:
        # без start_time VK сам режет выдачу до последних 24 часов
        start_time = int(time.time() - float(settings["default_days"]) * 86400)
    if start_time and end_time and start_time >= end_time:
        raise SystemExit("--since должен быть меньше --until")

    log(
        f"старт: тег={args.tag}, страниц={pages}, по {count}, проходов={passes}, "
        f"пауза {pause} с, с {start_time or '—'} по {end_time or '—'}"
    )
    # Чем искать тег — решает настройка tag_query (страница «Теги»); флаг перебивает
    # её. Режим ставим до первого запроса; «авто» разрешается ниже пробой по тегу.
    mode = set_tag_query(args.tag_query if args.tag_query is not None else settings["tag_query"])
    log(f"тег в запросе: {TAG_QUERY_LABELS[mode]}")
    try:
        client = build_client(args)
    except VkAuthError as exc:
        log(f"нет доступа к VK API: {exc}")
        log("сначала выполните вход: python src/vk_auth.py --login")
        return 1
    log(f"сессия: {client.login or '—'}, прокси: {client.proxy or '—'}")
    if mode == "auto":
        # Авто-режим: проба одного тега — самое место проверить оба запроса и
        # показать, где выдача больше (в прогоне это делает run_tags.tag_mode_for).
        clean = extract_tag(args.tag)
        try:
            mode, hash_items, word_items = detect_tag_mode(client, clean)
        except Exception as exc:  # noqa: BLE001 — проба не должна валить запуск
            log(f"режим тега не определился ({type(exc).__name__}: {exc}) — ищу хештегом")
            mode = set_tag_query("hash")
        else:
            mode = set_tag_query(mode)
            log(
                f"#{clean}: за {MODE_PROBE_HOURS} ч хештегом {hash_items}, словом "
                f"{word_items} — ищу {'словом' if mode == 'word' else 'хештегом'}"
            )
    db = None if args.dry_run else DB(
        dedupe=settings["dedupe_posts"], min_text_chars=settings["min_text_chars"]
    )
    summary = run_tag(
        client,
        db,
        args.tag,
        pages=pages,
        count=count,
        passes=passes,
        repeat_pause=repeat_pause,
        start_time=start_time,
        end_time=end_time,
        resume=not args.no_resume,
        dry_run=args.dry_run,
        stop_when_no_new=stop_when_no_new,
        json_log=args.json,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
