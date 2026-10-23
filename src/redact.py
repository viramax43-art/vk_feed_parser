#!/usr/bin/env python3
"""Маскировка секретов в логах: cookies, токены, пароли прокси, номера аккаунтов.

Зачем: лог прогона — главный след «почему встал сбор», и его пересылают, кладут в
тикет, показывают в чате. В нём же оказываются cookie сессии VK (Playwright печатает
заголовки запроса, когда обмен cookies на токен падает по таймауту), полный адрес
прокси с логином и паролем (`http://user:pass@host:port`) и телефон аккаунта.

Маскировка одна на весь проект: `redact()` вызывают лог панели (src/webui.py) и лог
входа (src/vk_auth.py), поэтому и в памяти панели, и в `data/logs/panel-*.log`
секретов нет. Полезное для разбора остаётся: host:port прокси, ошибка VK, время,
номера окон и постов. Телефон виден хвостом (`+7…0853`) — по нему аккаунты
различимы, но самого номера в логе нет.

Готовые логи можно почистить этой же функцией:

    python src/redact.py data/logs/panel-20261023.log

Файл переписывается на месте, строки без секретов не меняются; в конце печатается,
сколько строк изменено.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MASK = "<скрыто>"
_PROJECT = Path(__file__).resolve().parent.parent
_HOME = Path.home()

# `- cookie: remixstid=…; remixstlid=…` — так Playwright печатает заголовки запроса
# в тексте ошибки (APIRequestContext.post: Timeout …). Маскируем строку целиком:
# имён cookie там десятки, и все они — сессия.
_COOKIE_HEADER = re.compile(r"(?im)^([ \t]*(?:[-*][ \t]*)?cookie[ \t]*:[ \t]*).*$")

# Значения cookie сессии VK в любом виде: `remixsid=…`, `"remixsid": "…"`,
# `remixsid: …`. «да»/«нет» не трогаем — это наш собственный отчёт о наличии куки.
_VK_COOKIE = re.compile(
    r"(?i)\b((?:remixsid6?|remixstid|remixstlid|remixnsid|remixlhk|remixgid|remixmst|"
    r"remixua|remixsuc|httoken|sui|tmr_lvid|tmr_lvidts)[\"']?[ \t]*[=:][ \t]*[\"']?)"
    r"(?!да\b|нет\b)([^\s,;}\"'&]+)"
)

# user access_token: `vk1.a.…` где угодно и `access_token=…`/`"access_token": "…"`.
_VK_TOKEN = re.compile(r"vk1\.a\.[A-Za-z0-9_.\-]{20,}")
_ACCESS_TOKEN = re.compile(r"(?i)(access_token[\"']?[ \t]*[=:][ \t]*[\"']?)[A-Za-z0-9_.\-]{8,}")

# Заголовки авторизации: `Authorization: Bearer …`, `Proxy-Authorization: Basic …`
_AUTH_HEADER = re.compile(r"(?i)((?:proxy-)?authorization[ \t]*:[ \t]*)(?:basic|bearer)[ \t]+\S+")

# Прокси с логином и паролем: url-вид и dump-вид `95.181.155.39:8000:user:pass`.
# host:port остаётся — по нему видно, с какого адреса на какой перешли.
_PROXY_URL = re.compile(r"(?i)\b((?:https?|socks4a?|socks5h?)://)[^/\s:@]+:[^/\s@]+@")
_PROXY_DUMP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}):[^\s:]+:\S+")

# Файл сессии называется по номеру: `session_79047490853.json`.
_SESSION_FILE = re.compile(r"(?i)(session_)(\d{10,11})(?=\.json)")

# Телефон аккаунта: +79047490853, 79047490853, 89047490853. Хвост из четырёх цифр
# оставляем — иначе в логе не понять, какой из аккаунтов делает окно.
_PHONE = re.compile(r"(?<![\d+])\+?([78])(\d{10})(?!\d)")


def _mask_paths(text: str) -> str:
    """Убирает имя пользователя из путей: `C:\\Users\\armian\\…` → `~\\…`."""
    for raw in {str(_PROJECT), str(_PROJECT).replace("\\", "/")}:
        text = text.replace(raw, "<проект>")
    for raw in {str(_HOME), str(_HOME).replace("\\", "/")}:
        sep = "\\" if "\\" in raw else "/"
        text = text.replace(raw + sep, "~" + sep)
    return text


def _mask_phone(match: re.Match) -> str:
    return "+7…" + match.group(2)[-4:]


def redact(text: str) -> str:
    """Возвращает строку без секретов. Повторный вызов ничего не меняет."""
    if not text:
        return text
    out = _mask_paths(text)
    out = _COOKIE_HEADER.sub(r"\1" + MASK, out)
    out = _VK_COOKIE.sub(r"\1" + MASK, out)
    out = _VK_TOKEN.sub("vk1.a." + MASK, out)
    out = _ACCESS_TOKEN.sub(r"\1" + MASK, out)
    out = _AUTH_HEADER.sub(r"\1" + MASK, out)
    out = _PROXY_URL.sub(r"\1" + MASK + "@", out)
    out = _PROXY_DUMP.sub(r"\1:" + MASK + ":" + MASK, out)
    out = _SESSION_FILE.sub(r"\1<номер>", out)
    out = _PHONE.sub(_mask_phone, out)
    return out


def scrub_file(path) -> int:
    """Маскирует секреты в готовом логе на месте. Возвращает число изменённых строк."""
    target = Path(path)
    text = target.read_text(encoding="utf-8", errors="replace")
    changed = 0
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        clean = redact(line)
        if clean != line:
            changed += 1
        out.append(clean)
    if changed:
        target.write_text("".join(out), encoding="utf-8")
    return changed


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    total = 0
    for name in argv:
        target = Path(name)
        if not target.is_file():
            print(f"{name}: файла нет")
            continue
        done = scrub_file(target)
        total += done
        print(f"{name}: изменено строк {done}")
    print(f"итого: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
