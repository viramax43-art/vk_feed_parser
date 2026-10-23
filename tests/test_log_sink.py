#!/usr/bin/env python3
"""Проверка файла лога панели: строки ложатся на диск, файл режется по размеру,
а секреты (cookie, токены, пароли прокси, телефон) в него не попадают.

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
import redact as redact_mod  # noqa: E402

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

# 5. секреты до файла не доходят: cookies, токены, пароли прокси, телефон
mask = redact_mod.MASK
proxy_line = "прокси: http://u7Xk2:pAsS-99@95.181.155.39:8000"
clean = redact_mod.redact(proxy_line)
check(
    "u7Xk2" not in clean and "pAsS-99" not in clean and "95.181.155.39:8000" in clean,
    f"пароль прокси скрыт, адрес остался: {clean}",
)
dump_line = "прокси: 95.181.155.39:8000:u7Xk2:pAsS-99"
clean = redact_mod.redact(dump_line)
check(
    "u7Xk2" not in clean and "pAsS-99" not in clean and "95.181.155.39:8000" in clean,
    f"прокси в dump-виде тоже закрыт: {clean}",
)
cookie_line = "cookie: remixstid=955354769_oUxc49kZ; remixsid=1_8_7-RW4FY70NrSbpd8; httoken=eKcgpsW2S4dst"
clean = redact_mod.redact(cookie_line)
check(
    "955354769_oUxc49kZ" not in clean and "1_8_7-RW4FY70NrSbpd8" not in clean,
    f"cookie из ошибки Playwright не видно: {clean}",
)
pair_line = "сессия: remixsid=1_8_7-RW4FY70NrSbpd8FDCIr04o8, httoken=eKcgpsW2S4dst"
clean = redact_mod.redact(pair_line)
check(
    "1_8_7-RW4FY70" not in clean and "eKcgpsW2S4dst" not in clean and mask in clean,
    f"значения cookie закрыты и вне заголовка: {clean}",
)
token_line = "токен: vk1.a.8QjSATLs9Bm3IvbCx8BwIdzEkPFOUQAJ0RL3CVL63uO4A"
clean = redact_mod.redact(token_line)
check("8QjSATLs9Bm3" not in clean, f"user token скрыт: {clean}")
access_line = "POST https://login.vk.com/?act=web_token&access_token=vk1.a.SecretValueHere&x=1"
clean = redact_mod.redact(access_line)
check("SecretValueHere" not in clean, f"access_token в адресе скрыт: {clean}")
phone_line = "79047490853: #маникюр 23.10 10:09:20–23.10 11:39:20: постов 25, новых 0 (end_of_feed)"
clean = redact_mod.redact(phone_line)
check(
    "79047490853" not in clean and "+7…0853" in clean and "23.10 10:09:20" in clean,
    f"телефон закрыт, время окна на месте: {clean}",
)
path_line = f"сессия сохранена: {ROOT}\\data\\session_79047490853.json"
clean = redact_mod.redact(path_line)
check(
    str(ROOT) not in clean and "session_<номер>.json" in clean,
    f"путь и номер в имени файла сессии закрыты: {clean}",
)
own = "[16:22:20] 79029770529: сессия обновлена (всего 2 обменов)"
check(redact_mod.redact(redact_mod.redact(own)) == redact_mod.redact(own), "повторная чистка ничего не ломает")
keep = "F5 не помог — меняю прокси 194.226.60.6:8000 → 95.181.157.227:8000, беру окно заново"
check(redact_mod.redact(keep) == keep, f"адреса без пароля остаются как есть: {redact_mod.redact(keep)}")
counts = "cookies:   12 шт, remixsid=да"
check(redact_mod.redact(counts) == counts, f"счётчик cookie и «remixsid=да» не маскируются: {redact_mod.redact(counts)}")

# 6. Runner чистит и буфер панели, и файл — независимо от того, кто печатал строку
runner.log(proxy_line)
runner.log(phone_line)
panel_lines = " ".join(line for _seq, line in runner._lines)
runner_text = runner.log_file.path.read_text(encoding="utf-8")
check(
    "u7Xk2" not in panel_lines and "u7Xk2" not in runner_text,
    "пароль прокси не попал ни в панель, ни в файл",
)
check(
    "79047490853" not in panel_lines and "79047490853" not in runner_text,
    "телефон не попал ни в панель, ни в файл",
)

# 7. готовый лог чистится на месте (этим же чинят уже присланные логи)
legacy = tmp / "panel-legacy.log"
legacy.write_text(
    "[16:08:21] прокси: http://u7Xk2:pAsS-99@95.181.155.39:8000\n"
    "[16:09:05]   79047490853: ready\n"
    "[16:10:03] постов 25, новых 1 (end_of_feed)\n",
    encoding="utf-8",
)
changed = redact_mod.scrub_file(legacy)
legacy_text = legacy.read_text(encoding="utf-8")
check(changed == 2, f"чистка нашла ровно две строки с секретами: {changed}")
check("pAsS-99" not in legacy_text and "79047490853" not in legacy_text, "в файле не осталось секретов")
check("постов 25, новых 1" in legacy_text, "строки без секретов не изменились")
check(redact_mod.scrub_file(legacy) == 0, "повторная чистка уже чистого файла ничего не меняет")

print("-" * 50)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "ЛОГ ПАНЕЛИ ПИШЕТСЯ В ФАЙЛ И ЧИСТИТСЯ ОТ СЕКРЕТОВ")
raise SystemExit(1 if FAILED else 0)
