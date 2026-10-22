"""Настройки прогона: data/settings.json (потоки, страницы, окна, паузы)."""
from __future__ import annotations

import argparse
import json

from paths import DATA_DIR

CONFIG_FILE = DATA_DIR / "settings.json"
DEFAULTS = {
    "max_accounts": 2,
    "retry_unfinished": False,
    "report_every_accounts": 10,
    "export_interval_sec": 15,
    "request_pause_sec": 0.75,
    "page_count": 200,
    "max_pages_per_tag": 50,
    "repeat_passes": 1,
    "repeat_pause_sec": 30,
    "stop_when_no_new_pages": 2,
    # Окна времени: тег режется на куски, куски раздаются всем аккаунтам.
    # Прогон идёт, пока период не пройден целиком: окна выдаются порциями
    # (воркеров × windows_per_worker), теги — по кругу.
    "default_days": 30,  # период, если не задан --hours/--days/--since
    "window_hours": 6,  # размер стартового окна
    "min_window_minutes": 1,  # мельче не дробим
    "job_attempts": 3,  # попыток на окно при сетевых/API-ошибках
    "repeat_overlap_sec": 600,  # нахлёст повторного прохода назад
    "split_if_seen": 30,  # окно вернуло >= N постов — делим пополам
    "windows_per_worker": 3,  # сколько окон держать в очереди на каждый поток
    # Лента меняется: свежий хвост периода перепроверяем в каждом запуске —
    # подтягиваются запоздавшие (ещё не проиндексированные ВК) посты и свежие
    # лайки/комментарии. 0 — выключено (тогда уже собранные часы пропускаются).
    "refresh_tail_hours": 6,
    # Токен VK живёт ограниченное время (в сессии — token_expires_at, обычно
    # около суток), а прогон «за 30 суток» идёт часами. Пока идёт работа, фон
    # раз в N минут обменивает cookies аккаунта на свежий access_token и
    # подставляет его работающим клиентам — живые аккаунты больше не выбывают
    # посреди прогона, и «Старт» не встаёт до ручного входа. Не чаще 15 минут:
    # частый обмен (каждые 2 мин) открывает браузер и раздражает VK — он начинает
    # молча резать выдачу. 0 — не обновлять.
    "session_refresh_minutes": 15,
    # Прогон не запустился из-за сессий («код 2»: готовых аккаунтов нет) — панель
    # сама открывает очередь входа: окна идут по одному, как у кнопки «Войти во
    # все, кому нужен вход». Не хотите окон — false, входить будете сами.
    "auto_login_on_fail": True,
    # Эмулятор браузера (профиль ленты + F5) работает на виду: окно браузера
    # открывается, и видно, как он заходит в ленту, жмёт F5 и меняет cookies на
    # свежий токен. false (по умолчанию) — окно скрыто, прогон идёт молча.
    # Включать, когда хочется глазами проверить, что делает эмулятор.
    "show_browser": False,
    # Скорость: интервал между запросами ОДНОГО аккаунта (VK режет всплески
    # кодами 6/9). Это главный тормоз прогона: 3.5–4.5 с на запрос с аккаунта.
    # Со своим прокси на каждый аккаунт можно снижать до 1.5–2.5 с.
    "min_call_gap_sec": 3.5,
    "max_call_gap_sec": 4.5,
    # Дубликаты: "off" — писать всё подряд; "tag" — не писать пост, текст
    # которого уже собран по этому тегу; "global" — ни по одному тегу базы.
    # Сравнение идёт по нормализованному тексту (без ссылок, хештегов, знаков).
    "dedupe_posts": "tag",
    # Осмысленный текст короче N знаков (хештеги/ссылки/упоминания не считаются)
    # не пишем вовсе: так отсеивается мусор вида «#ногти #маникюр». 0 — выкл.
    "min_text_chars": 0,
    # Чем искать тег в запросе к VK: "hash" — ищем хештег (#тег, как было всегда),
    # "word" — ищем слово без решётки (посты, где тег встречается в тексте),
    # "auto" — решаем по каждому тегу отдельно: VK отдаёт по «#тег» и по «тег»
    # разные выдачи, и у нишевых тегов хештег почти пустой (люди пишут слово без
    # решётки) — прогон по хештегу выглядит как «0 постов». В «авто» тег один раз
    # проверяется двумя запросами и выбор запоминается в базе (tags.query_mode).
    # Режим выбирают на странице «Теги» («с #» / «без #» / «авто»).
    "tag_query": "hash",
}

# Как режим тега подписан в интерфейсе: страница «Теги» в панели и в окне.
TAG_QUERY_LABELS = {
    "hash": "с # (хештег)",
    "word": "без # (слово)",
    "auto": "авто (по тегу)",
}


def normalize_tag_query(value: object) -> str:
    """Чем искать тег: "hash" — #тег (как было), "word" — слово без решётки, "auto".

    Значение приходит и из файла настроек (его правят руками), и из панели, и из
    флага --tag-query, поэтому понимаем его и словами: «без #», «слово», «word» —
    без решётки; «с #», «хештег», «hash» — с решёткой; «авто», «по тегу», «auto» —
    выбор по каждому тегу (см. run_tags.tag_mode_for). Непонятное значение — это
    прежнее поведение (хештег): так старые настройки и команды работают как раньше.
    """
    text = str(value if value is not None else "").strip().lower()
    if text in ("auto", "авто", "по тегу", "автоматически", "автомат"):
        return "auto"
    if text in ("word", "words", "text", "слово", "словом", "без #", "без", "no", "0", "false", "off"):
        return "word"
    return "hash"


def tag_query_value(label: str) -> str:
    """Режим по подписи из интерфейса: «с # (хештег)» / «без # (слово)».

    Нужен спискам-выпадашкам: в них показываются подписи, а в настройку и в
    запрос к VK должен уходить "hash" / "word".
    """
    for mode, text in TAG_QUERY_LABELS.items():
        if text == str(label or "").strip():
            return mode
    return normalize_tag_query(label)


def as_bool(value: object, default: bool = False) -> bool:
    """Значение из settings.json как флаг.

    В JSON это true/false, но файл правят руками, поэтому человекочитаемые
    строки («on», «off», «нет») тоже понимаем.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "нет")
    if value is None:
        return bool(default)
    return bool(value)


def load_settings() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS)
    out.update(data)
    out["max_accounts"] = max(1, int(out["max_accounts"]))
    out["report_every_accounts"] = max(1, int(out.get("report_every_accounts", 10)))
    out["export_interval_sec"] = max(5, int(out.get("export_interval_sec", 15)))
    out["request_pause_sec"] = max(0.0, float(out.get("request_pause_sec", 0.4)))
    out["page_count"] = min(200, max(10, int(out.get("page_count", 50))))
    out["max_pages_per_tag"] = max(1, int(out.get("max_pages_per_tag", 10)))
    out["repeat_passes"] = max(1, int(out.get("repeat_passes", 1)))
    out["repeat_pause_sec"] = max(0, int(out.get("repeat_pause_sec", 60)))
    out["stop_when_no_new_pages"] = max(1, int(out.get("stop_when_no_new_pages", 2)))
    out["default_days"] = max(0.01, float(out.get("default_days", 30)))
    out["window_hours"] = max(0.1, float(out.get("window_hours", 24)))
    out["min_window_minutes"] = max(1, int(out.get("min_window_minutes", 10)))
    out["job_attempts"] = max(1, int(out.get("job_attempts", 3)))
    out["repeat_overlap_sec"] = max(0, int(out.get("repeat_overlap_sec", 600)))
    out["split_if_seen"] = max(1, int(out.get("split_if_seen", 30)))
    out["refresh_tail_hours"] = max(0.0, float(out.get("refresh_tail_hours", 6)))
    out["session_refresh_minutes"] = min(
        1440.0, max(0.0, float(out.get("session_refresh_minutes", 2)))
    )
    # true/false из JSON, но человекочитаемые строки («off», «нет») тоже понимаем:
    # файл настроек люди правят руками.
    out["auto_login_on_fail"] = as_bool(out.get("auto_login_on_fail"), True)
    out["show_browser"] = as_bool(out.get("show_browser"), False)
    out["min_call_gap_sec"] = max(0.0, float(out.get("min_call_gap_sec", 7.0)))
    out["max_call_gap_sec"] = max(
        out["min_call_gap_sec"], float(out.get("max_call_gap_sec", 9.0))
    )
    dedupe = str(out.get("dedupe_posts", "tag") or "").strip().lower()
    out["dedupe_posts"] = dedupe if dedupe in ("off", "tag", "global") else "tag"
    out["min_text_chars"] = max(0, int(out.get("min_text_chars", 0)))
    # Тег в запросе к VK («с #» / «без #»): файл правят и руками, поэтому значение
    # приводим к "hash"/"word", а непонятное считаем прежним поведением.
    out["tag_query"] = normalize_tag_query(out.get("tag_query", "hash"))
    return out


def save_settings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-accounts", type=int)
    p.add_argument("--repeat-passes", type=int)
    p.add_argument("--page-count", type=int)
    p.add_argument("--call-gap", type=float, help="мин. интервал между запросами аккаунта, с")
    p.add_argument("--call-gap-max", type=float, help="макс. интервал между запросами аккаунта, с")
    p.add_argument("--session-refresh-minutes", type=float,
                   help="как часто менять access_token аккаунтов во время прогона, мин")
    p.add_argument("--auto-login-on-fail", choices=("on", "off"),
                   help="панели самой открывать очередь входа, если прогон не запустился")
    p.add_argument("--show-browser", choices=("on", "off"),
                   help="показывать браузер во время прогона: окно обхода ленты "
                        "(какое окно времени берётся) и окно эмулятора, когда он "
                        "освежает ленту и берёт токен")
    p.add_argument("--dedupe-posts", choices=("off", "tag", "global"))
    p.add_argument("--min-text-chars", type=int)
    p.add_argument("--tag-query", choices=("hash", "word", "auto"),
                   help="чем искать тег: hash — с решёткой (#тег, как было всегда), "
                        "word — словом, без решётки (посты, где тег встречается в "
                        "тексте), auto — выбрать по каждому тегу (где выдача больше)")
    p.add_argument("--show", action="store_true")
    args = p.parse_args()
    settings = load_settings()
    touched = False
    if args.max_accounts is not None:
        settings["max_accounts"] = max(1, args.max_accounts)
        touched = True
    if args.repeat_passes is not None:
        settings["repeat_passes"] = max(1, args.repeat_passes)
        touched = True
    if args.page_count is not None:
        settings["page_count"] = min(200, max(10, args.page_count))
        touched = True
    if args.call_gap is not None:
        settings["min_call_gap_sec"] = max(0.0, args.call_gap)
        touched = True
    if args.call_gap_max is not None:
        settings["max_call_gap_sec"] = max(0.0, args.call_gap_max)
        touched = True
    if args.session_refresh_minutes is not None:
        settings["session_refresh_minutes"] = min(1440.0, max(0.0, args.session_refresh_minutes))
        touched = True
    if args.auto_login_on_fail is not None:
        settings["auto_login_on_fail"] = args.auto_login_on_fail == "on"
        touched = True
    if args.show_browser is not None:
        settings["show_browser"] = args.show_browser == "on"
        touched = True
    if args.dedupe_posts is not None:
        settings["dedupe_posts"] = args.dedupe_posts
        touched = True
    if args.min_text_chars is not None:
        settings["min_text_chars"] = max(0, args.min_text_chars)
        touched = True
    if args.tag_query is not None:
        settings["tag_query"] = normalize_tag_query(args.tag_query)
        touched = True
    if touched:
        save_settings(settings)
    if args.show or not touched:
        print(json.dumps(settings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
