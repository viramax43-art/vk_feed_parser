#!/usr/bin/env python3
"""Лимиты newsfeed.search: count 1..200, первые 1000 результатов, total_count.

    python tests/test_search_limits.py

Сеть не нужна: VK подменяется заглушками. Проверяем ровно те места, из-за
которых окно могло упасть или потерять посты:

  1) count из настроек/консоли обрезается до 200 — на большее VK отвечает
     ошибкой 100, и окно падало бы целиком;
  2) страницы листаются next_from → start_from (не offset), а в запрос не
     подмешиваются параметры, которых у метода нет (sort, случайный «антикеш»);
  3) окно, дошедшее до первых 1000 записей, помечается need_split: глубже VK
     не отдаёт, и остаток окна собирают половинки, а не повторяющиеся страницы;
  4) «всего у VK» берётся из документированного total_count (с запасными
     вариантами), а не теряется.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import probe  # noqa: E402
from vk_client import MAX_COUNT, MAX_RESULTS, VkFeedClient  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


class CaptureClient(VkFeedClient):
    """Клиент без сети: запоминает параметры запроса и отдаёт заданный ответ."""

    def __init__(self) -> None:
        super().__init__("vk1.a.тест")
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict = {"items": [], "next_from": ""}

    def call(self, method, params, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append((method, dict(params)))
        return self.answer


# 1. count: VK принимает не больше 200 записей за вызов.
client = CaptureClient()
for asked, expected in ((999, MAX_COUNT), (200, 200), (50, 50), (0, 1), (-5, 1)):
    client.search("маникюр", count=asked, start_time=1_700_000_000, end_time=1_700_003_600)
    method, params = client.calls[-1]
    check(
        params["count"] == expected,
        f"count {asked} → {expected} (запрос {method})",
    )

# 2. Никаких несуществующих у newsfeed.search параметров: сортировки и
#    «случайного» антикеша там нет, VK их игнорирует.
check(
    "sort" not in params and "offset" not in params,
    "в запросе нет sort/offset — только документированные параметры",
)
client.search("маникюр", count=200, start_from="cur9")
check(
    client.calls[-1][1].get("start_from") == "cur9",
    "следующая страница идёт через start_from (next_from прошлой страницы)",
)

# 4. total_count — документированное поле ответа.
client.answer = {"items": [], "next_from": "", "total_count": 4321}
check(client.search_hashtag("маникюр")["total"] == 4321, "всего у VK берётся из total_count")
client.answer = {"items": [], "next_from": "", "count": 777}
check(client.search_hashtag("маникюр")["total"] == 777, "без total_count — запасной count")
client.answer = {"items": [], "next_from": ""}
check(client.search_hashtag("маникюр")["total"] == 0, "без счётчиков — по факту отданного")


class PagedClient:
    """Отдаёт per_page постов на страницу; next_from есть на каждой странице."""

    def __init__(self, per_page: int) -> None:
        self.per_page = per_page
        self.calls: list[str | None] = []

    def search_hashtag(
        self, tag, *, count=50, start_from=None, start_time=None, end_time=None, mode=None
    ):  # noqa: ANN001
        self.calls.append(start_from)
        page = len(self.calls)
        items = [
            {
                "id": page * 100000 + i,
                "owner_id": -1,
                "date": int(start_time or 0) + i,
                "text": f"пост {page}/{i}",
            }
            for i in range(self.per_page)
        ]
        return {"tag": tag, "items": items, "next_from": f"cur{page}", "total": 100000}


# 3. Окно доходит ровно до 1000 записей и дальше не листает: VK глубже не отдаёт.
paged = PagedClient(100)
res = probe.run_window(
    paged, None, "маникюр",
    start_time=1_700_000_000, end_time=1_700_021_600,
    count=200, max_pages=50, split_if_seen=30,
)
check(res["status"] == "ok", "окно не сломалось на лимите")
check(res["seen"] == MAX_RESULTS, f"остановились на {MAX_RESULTS} записях (получено {res['seen']})")
check(res["pages"] == MAX_RESULTS // 100, f"страниц {res['pages']} — лишних запросов нет")
check(res["reason"] == "capped", "причина — capped (остаток окна заберут половинки)")
check(res["need_split"] is True, "окно помечено need_split — остаток не теряется")
check(
    paged.calls[:3] == [None, "cur1", "cur2"],
    "страницы идут next_from → start_from, первая — без курсора",
)

# Граница не срабатывает раньше времени: 990 записей — ещё листаем.
paged = PagedClient(99)
res = probe.run_window(
    paged, None, "маникюр",
    start_time=1_700_000_000, end_time=1_700_021_600,
    count=200, max_pages=50, split_if_seen=30,
)
check(res["seen"] == 1089 and res["pages"] == 11, "на 990 записях листание не обрывается")
check(res["need_split"] is True, "и на этой границе окно всё равно делится")


print("-" * 50)
print("ПРОВАЛ: " + "; ".join(FAILED) if FAILED else "ЛИМИТЫ VK УЧТЕНЫ")
raise SystemExit(1 if FAILED else 0)
