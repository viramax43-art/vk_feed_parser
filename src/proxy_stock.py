"""Пачка прокси: запас свободных прокси и привязка 1:1 к аккаунтам без своего прокси.

Правила:
  * запас хранится в data/proxy_stock.txt, одна строка — один прокси;
  * прокси из запаса не дублируются ни внутри запаса, ни с уже привязанными аккаунтами;
  * привязка: аккаунт без своего прокси получает ровно один свободный прокси из запаса;
    никакой раздачи по кругу — если прокси не хватает, лишние аккаунты остаются без прокси;
  * строки-заголовки («Vyacheslav:») и строки «телефон:пароль» пропускаются.

Ещё запас работает как склад подмены: swap_account_proxy отдаёт аккаунту свободный
прокси, когда VK молча режет выдачу и F5 не помог (адрес мог быть помечен), а прежний
адрес возвращает в запас — он снова свободен.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from accounts import account_key, load_accounts, normalize_proxy, save_accounts
from paths import ACCOUNTS_FILE, DATA_DIR

STOCK_FILE = DATA_DIR / "proxy_stock.txt"

# Итоговый вид после normalize_proxy: scheme://[user:pass@]host:port
PROXY_OK = re.compile(r"^(https?|socks4|socks5h?)://(\S+@)?[^\s:/@]+:\d{1,5}$")
# «79990000000:пароль» — это строка аккаунта, а не прокси
ACCOUNT_LIKE = re.compile(r"^\+?\d{10,15}:\S")
# «host:port» или «host:port» с логином — это прокси, даже если логин похож на телефон
HOST_PORT = re.compile(r"^[\w.-]+:\d{1,5}$|^\S+@[\w.-]+:\d{1,5}$")


def read_stock(path: Optional[Path] = None) -> list[str]:
    """Прокси из запаса (data/proxy_stock.txt): по строке на адрес, комментарии — мимо."""
    file = Path(path or STOCK_FILE)
    if not file.exists():
        return []
    out: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def write_stock(items: list[str], path: Optional[Path] = None) -> None:
    """Переписывает запас: заголовок + по строке на прокси."""
    file = Path(path or STOCK_FILE)
    file.parent.mkdir(parents=True, exist_ok=True)
    head = "# свободные прокси: одна строка — один прокси. Привязываются к аккаунтам без своего.\n"
    file.write_text(head + "".join(f"{p}\n" for p in items), encoding="utf-8")


def _account_proxies() -> set[str]:
    return {normalize_proxy(a.proxy_raw) for a in load_accounts(ACCOUNTS_FILE) if (a.proxy_raw or "").strip()}


def parse_batch(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Разбирает пачку. Возвращает (прокси по порядку без повторов внутри пачки, [(строка, причина)])."""
    valid: list[str] = []
    invalid: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if s.endswith(":") and "@" not in s:  # заголовок блока, например «Vyacheslav:»
            continue
        if ACCOUNT_LIKE.match(s) and not HOST_PORT.match(s):
            invalid.append((s, "похоже на аккаунт (телефон:пароль), не прокси"))
            continue
        proxy = normalize_proxy(s)
        if not proxy:
            invalid.append((s, "пустая строка или слово «без прокси»"))
            continue
        if not PROXY_OK.match(proxy):
            invalid.append((s, "не распознан как прокси (нужно host:port или user:pass@host:port)"))
            continue
        if proxy in seen:
            continue
        seen.add(proxy)
        valid.append(proxy)
    return valid, invalid


def add_proxy_batch(text: str) -> dict:
    """Добавляет пачку в запас. Дубли (в запасе или уже у аккаунтов) пропускаются."""
    valid, invalid = parse_batch(text)
    stock = read_stock()
    known = set(stock) | _account_proxies()
    new = [p for p in valid if p not in known]
    if new:
        write_stock(stock + new)
    return {
        "parsed": len(valid),
        "added": len(new),
        "duplicates": len(valid) - len(new),
        "invalid": invalid,
        "stock": len(stock) + len(new),
    }


def bind_free_proxies() -> dict:
    """Привязывает свободные прокси из запаса к аккаунтам без своего — по одному на аккаунт."""
    accounts = load_accounts(ACCOUNTS_FILE)
    used = _account_proxies()
    # прокси, уже стоящий у аккаунта, из запаса убираем — он не свободный
    stock = [p for p in read_stock() if p not in used]
    need = [a for a in accounts if not (a.proxy_raw or "").strip()]
    bound: list[str] = []
    while need and stock:
        acc = need.pop(0)
        acc.proxy_raw = stock.pop(0)
        bound.append(acc.login)
    if bound:
        save_accounts(ACCOUNTS_FILE, accounts)
    write_stock(stock)
    return {
        "bound": len(bound),
        "bound_logins": bound,
        "without_proxy": len(need),
        "stock_left": len(stock),
    }


def swap_account_proxy(
    login: str,
    current: str = "",
    accounts_path: Optional[Path] = None,
    stock_path: Optional[Path] = None,
) -> dict:
    """Меняет аккаунту адрес на свободный из запаса — проба другого IP.

    Нужно, когда VK молча отдаёт пустую выдачу и освежение сессии (F5) не помогло:
    выдача пустая не из-за сессии, а потому что адрес помечен. Аккаунт получает
    первый свободный прокси из запаса (data/proxy_stock.txt), запись в accounts.txt
    обновляется, а прежний адрес возвращается в запас в конец — он снова свободен и
    достанется кому-то последним. Совсем сожжённый адрес удалите из файла запаса
    руками: смена прокси — единственное место, где прогон правит accounts.txt сам.

    Возвращает {"ok", "reason", "old", "new", "stock_left"}; ok=False — смены не
    было (аккаунта нет в файле, запас пуст): прогон пишет причину в лог и работает
    дальше, окно это не тормозит.
    """
    path = Path(accounts_path or ACCOUNTS_FILE)
    file = Path(stock_path or STOCK_FILE)
    key = account_key(login)
    old = normalize_proxy(current)
    accounts = load_accounts(path)
    target = next((a for a in accounts if account_key(a.login) == key), None)
    if target is None:
        return {"ok": False, "reason": "аккаунта нет в accounts.txt",
                "old": old, "new": "", "stock_left": len(read_stock(file))}
    if not old:
        old = normalize_proxy(target.proxy_raw)
    # Свободный — тот, что не стоит ни у одного аккаунта: один адрес на аккаунт.
    used = {normalize_proxy(a.proxy_raw) for a in accounts if (a.proxy_raw or "").strip()}
    stock = [p for p in read_stock(file) if p not in used]
    if not stock:
        return {"ok": False, "reason": "в запасе нет свободных прокси",
                "old": old, "new": "", "stock_left": 0}
    new = stock.pop(0)
    if old and old != new:
        stock.append(old)
    target.proxy_raw = new
    save_accounts(path, accounts)
    write_stock(stock, file)
    return {"ok": True, "reason": "", "old": old, "new": new, "stock_left": len(stock)}
