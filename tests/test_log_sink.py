#!/usr/bin/env python3
"""Проверка файла лога панели: строки ложатся на диск и файл режется по размеру.

    python tests/test_log_sink.py

Сеть и браузер не нужны: пишем во временный каталог, настоящий data/logs не трогаем.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import webui  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)


tmp = Path(tempfile.mkdtemp(prefix="log_sink_"))

# 1. файл появляется только вместе с первой строкой
sink = webui.LogFile(tmp / "logs")
check(not sink.path.exists(), "до первой строки файл не создаётся")
check(sink.path.name.startswith("panel-") and sink.path.suffix == ".log", f"имя файла лога: {sink.path.name}")

sink.write("первая строка")
text = sink.path.read_text(encoding="utf-8")
check(text == "первая строка\n", f"строка записана целиком: {text!r}")

sink.write("вторая строка\n")
text = sink.path.read_text(encoding="utf-8")
check(text.count("\n") == 2, f"перевод строки не удваивается: {text!r}")

# 2. переполнение: заводится следующий файл, старый остаётся на месте
small = webui.LogFile(tmp / "logs", prefix="rotate", max_bytes=64 * 1024)
first = small.path
for _ in range(200):  # 200 × 512 Б ≈ 100 КиБ — предел в 64 КиБ перейдён
    small.write("x" * 512)
check(small.path != first, f"после переполнения пишем в новый файл: {small.path.name}")
check(small.path.name.endswith("-2.log"), f"в имени нового файла номер части: {small.path.name}")
check(first.exists() and first.stat().st_size >= 64 * 1024, "старый файл остался и не обрезан")
check(len(list((tmp / "logs").glob("rotate-*.log"))) == 2, "файлов ровно два: старый и новый")

# 3. каталог недоступен (на его месте файл) — запись не роняет панель
broken_parent = tmp / "broken"
broken_parent.write_text("не каталог", encoding="utf-8")
broken = webui.LogFile(broken_parent / "logs")
broken.write("строка")
broken.write("ещё строка")
check(broken._fh is None, "при ошибке диска файл закрыт, исключения нет")

# 4. Runner дублирует в файл всё, что показывает в панели
runner = webui.Runner(log_file=webui.LogFile(tmp / "runner"))
runner.log("проверка лога")
lines = [line for _seq, line in runner._lines]
check(any("проверка лога" in line for line in lines), "строка попала в буфер панели")
runner_text = runner.log_file.path.read_text(encoding="utf-8")
check("проверка лога" in runner_text, "та же строка попала в файл")
check(
    webui.status_payload(runner, 8787)["log_file"] == str(runner.log_file.path),
    "status отдаёт путь к файлу лога",
)

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ЛОГ ПАНЕЛИ ПИШЕТСЯ В ФАЙЛ")
raise SystemExit(1 if FAILED else 0)
