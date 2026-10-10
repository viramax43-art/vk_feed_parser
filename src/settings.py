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
    "request_pause_sec": 1.5,
    "page_count": 200,
    "max_pages_per_tag": 50,
    "repeat_passes": 1,
    "repeat_pause_sec": 60,
    "stop_when_no_new_pages": 2,
    # Окна времени: тег режется на куски, куски раздаются всем аккаунтам.
    "default_days": 30,  # период, если не задан --hours/--days/--since
    "window_hours": 6,  # размер стартового окна
    "min_window_minutes": 1,  # мельче не дробим
    "job_attempts": 3,  # попыток на окно при сетевых/API-ошибках
    "repeat_overlap_sec": 600,  # нахлёст повторного прохода назад
    "split_if_seen": 30,  # окно вернуло >= N постов — делим пополам
}


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
    if touched:
        save_settings(settings)
    if args.show or not touched:
        print(json.dumps(settings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
