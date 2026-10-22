#!/usr/bin/env python3
"""Аккаунты VK и прокси из data/accounts.txt.

Поддерживаемые строки (по одной на аккаунт):
  телефон;пароль;2FA_СЕКРЕТ;прокси
  телефон,пароль,2FA,прокси
  LOGIN:PASS:TOKEN
  LOGIN:PASS:TOKEN:прокси

Телефон — +7XXXXXXXXXX или цифры без «+». 2FA_СЕКРЕТ — base32 TOTP (можно пусто).
TOKEN — access_token VK (обычно vk1.a.…): при загрузке пишется в session_*.json,
браузерный вход не нужен. Прокси — host:port:user:pass, user:pass@ip:port,
http://… или socks5://…; пусто → строка proxies.txt с тем же номером; direct/none/-
→ без прокси. Строки с # или // в начале игнорируются.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from paths import ACCOUNTS_FILE, DATA_DIR, resolve_proxies_file

PROXY_SCHEME_RE = re.compile(r"^(https?|socks5h?|socks4)://", re.IGNORECASE)
VK_TOKEN_RE = re.compile(r"^vk\d+\.", re.IGNORECASE)
# login:pass:vkN.…[:proxy] — пароль без «:», токен без «:»
COLON_TOKEN_RE = re.compile(
    r"^([^:\s]+):([^:]*):(vk\d+\.[^\s:]+)(?::(\S+))?$",
    re.IGNORECASE,
)
DIRECT_WORDS = {"direct", "none", "no-proxy", "напрямую", "-"}  # «без прокси»


@dataclass
class Account:
    login: str
    password: str = ""
    twofa_secret: str = ""
    proxy_raw: str = ""
    access_token: str = ""
    line_no: int = 0
    session_file: Optional[Path] = None
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.login

    @property
    def session_path(self) -> Path:
        if self.session_file:
            return Path(self.session_file)
        raw = (self.login or "").strip()
        digits = re.sub(r"\D+", "", raw)
        base = digits or re.sub(r"[^0-9A-Za-z_.-]", "_", raw) or "session"
        return DATA_DIR / f"session_{base}.json"

    def otp(self) -> str:
        """Текущий TOTP-код, если задан секрет (нужен pyotp)."""
        secret = (self.twofa_secret or "").strip().replace(" ", "")
        if not secret:
            return ""
        try:
            import pyotp
        except ImportError:
            return ""
        try:
            return pyotp.TOTP(secret).now()
        except Exception:
            return ""


def _clean(line: str) -> str:
    return (line or "").strip().lstrip("\ufeff")


def is_comment(line: str) -> bool:
    s = _clean(line)
    return not s or s.startswith("#") or s.startswith("//")


def looks_like_vk_token(value: str) -> bool:
    return bool(VK_TOKEN_RE.match((value or "").strip()))


def account_file_line(acc: Account) -> str:
    """Строка для accounts.txt: токен в файл не кладём (он в session_*.json)."""
    return ";".join(
        [acc.login, acc.password or "", acc.twofa_secret or "", acc.proxy_raw or ""]
    )


def account_key(login: str) -> str:
    """Ключ уникальности: телефон по цифрам (8… → 7…), иначе логин без регистра."""
    raw = (login or "").strip()
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) >= 10:
        return digits
    return raw.casefold()


def merge_account(old: Account, new: Account) -> Account:
    """Обновляет поля существующего аккаунта; login в файле оставляем прежним."""
    return Account(
        login=old.login,
        password=new.password if new.password != "" else old.password,
        twofa_secret=new.twofa_secret if new.twofa_secret != "" else old.twofa_secret,
        proxy_raw=new.proxy_raw if new.proxy_raw != "" else old.proxy_raw,
        access_token=new.access_token or old.access_token,
        line_no=old.line_no,
        session_file=old.session_file,
    )


def save_accounts(path: Path, accounts: list[Account]) -> None:
    """Пишет accounts.txt без дубликатов (порядок списка сохраняется)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    lines = [
        "# телефон;пароль;2FA;прокси  или  LOGIN:PASS:TOKEN (токен → session_*.json)",
    ]
    for acc in accounts:
        key = account_key(acc.login)
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(account_file_line(acc))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compact_accounts_file(path: Path = ACCOUNTS_FILE) -> int:
    """Убирает из файла повторы одного логина. Возвращает, сколько строк сняли."""
    path = Path(path)
    if not path.exists():
        return 0
    raw: list[Account] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        acc = parse_account_line(line, idx)
        if acc:
            raw.append(acc)
    unique: list[Account] = []
    seen: set[str] = set()
    for acc in raw:
        key = account_key(acc.login)
        if key in seen:
            continue
        seen.add(key)
        unique.append(acc)
    removed = len(raw) - len(unique)
    if removed:
        save_accounts(path, unique)
    return removed


def upsert_accounts(path: Path, incoming: list[Account]) -> tuple[int, int, int]:
    """Дописывает/обновляет аккаунты без дублей. (добавлено, обновлено, без изменений)."""
    path = Path(path)
    # В одной вставке один логин — последний побеждает.
    batch: dict[str, Account] = {}
    for acc in incoming:
        batch[account_key(acc.login)] = acc

    existing = load_accounts(path)
    by_key = {account_key(acc.login): acc for acc in existing}
    order = [account_key(acc.login) for acc in existing]

    added = updated = unchanged = 0
    for key, acc in batch.items():
        if key not in by_key:
            ensure_token_session(acc)
            by_key[key] = Account(
                login=acc.login,
                password=acc.password,
                twofa_secret=acc.twofa_secret,
                proxy_raw=acc.proxy_raw,
                access_token="",
                line_no=acc.line_no,
            )
            order.append(key)
            added += 1
            continue
        old = by_key[key]
        merged = merge_account(old, acc)
        merged.access_token = ""  # в файл не пишем
        token_touch = bool(acc.access_token)
        if token_touch:
            # session_path от login в файле — не плодим session_ с другим написанием.
            ensure_token_session(
                Account(login=old.login, password=acc.password, access_token=acc.access_token)
            )
        if account_file_line(merged) == account_file_line(old) and not token_touch:
            unchanged += 1
            continue
        by_key[key] = merged
        updated += 1

    save_accounts(path, [by_key[key] for key in order if key in by_key])
    return added, updated, unchanged


def ensure_token_session(acc: Account) -> bool:
    """Если в строке был TOKEN — пишет/обновляет session_<login>.json. True = записали."""
    token = (acc.access_token or "").strip()
    if not token:
        return False
    path = acc.session_path
    try:
        if path.exists():
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("access_token") or "") == token:
                return False
    except (OSError, ValueError):
        pass
    # Локальный импорт: accounts.py используют и тесты без полного vk_auth.
    from vk_auth import save_session

    save_session(
        {
            "access_token": token,
            "token_expires_at": 0,  # срок неизвестен → token_alive считает живым
            "user_id": 0,
            "login": acc.login,
            "proxy": "",
            "cookies": {},
            "source": "import_token",
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        path,
    )
    return True


def parse_account_line(line: str, line_no: int = 0) -> Optional[Account]:
    s = _clean(line)
    if is_comment(s):
        return None

    # Формат выдачи LOGIN:PASS:TOKEN[:прокси]
    colon = COLON_TOKEN_RE.match(s)
    if colon:
        login, password, token, proxy = colon.groups()
        return Account(
            login=login.strip(),
            password=(password or "").strip(),
            twofa_secret="",
            proxy_raw=(proxy or "").strip(),
            access_token=token.strip(),
            line_no=line_no,
        )

    # Формат НОМЕР:ПАРОЛЬ (без токена): 79990000000:пароль, +7...:пароль
    phone_pass = re.match(r"^\+?(\d{10,15}):(.+)$", s)
    if phone_pass and not looks_like_vk_token(phone_pass.group(2)):
        login, password = phone_pass.group(1), phone_pass.group(2).strip()
        return Account(
            login=login,
            password=password,
            twofa_secret="",
            proxy_raw="",
            access_token="",
            line_no=line_no,
        )

    parts = [p.strip() for p in s.split(";")]
    if len(parts) == 1:
        # dump через запятую: телефон,пароль,2fa,прокси
        parts = [p.strip() for p in s.split(",")]
    if len(parts) < 2:
        return None
    login = parts[0]
    if not login:
        return None
    password = parts[1] if len(parts) > 1 else ""
    field3 = parts[2] if len(parts) > 2 else ""
    field4 = parts[3] if len(parts) > 3 else ""
    # Если в 3-м поле токен (а не TOTP) — это LOGIN;PASS;TOKEN;прокси
    twofa, token, proxy = field3, "", field4
    if looks_like_vk_token(field3):
        token, twofa = field3, ""
    elif looks_like_vk_token(field4) and not field3:
        token, proxy = field4, ""
    return Account(
        login=login,
        password=password,
        twofa_secret=twofa,
        proxy_raw=proxy,
        access_token=token,
        line_no=line_no,
    )


def delete_accounts(logins: list[str], path: Path = ACCOUNTS_FILE) -> tuple[int, Path]:
    import shutil
    """Удаляет строки аккаунтов из accounts.txt по ключу логина. Пишет бэкап .bak.

    Возвращает (сколько строк удалено, путь к бэкапу). Сессии и базу чистит
    вызывающий код, чтобы здесь не было побочных эффектов.
    """
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not path.exists():
        return 0, backup
    wanted = {account_key(login) for login in logins if login}
    shutil.copy2(path, backup)
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        acc = parse_account_line(line, 0)
        if acc and account_key(acc.login) in wanted:
            removed += 1
            continue
        kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
    return removed, backup


def load_accounts(path: Path = ACCOUNTS_FILE) -> list[Account]:
    """Читает accounts.txt; повторы одного логина пропускает (оставляет первую строку)."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[Account] = []
    seen: set[str] = set()
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        acc = parse_account_line(line, idx)
        if not acc:
            continue
        key = account_key(acc.login)
        if key in seen:
            continue
        seen.add(key)
        ensure_token_session(acc)
        out.append(acc)
    return out


def proxies_from_file(path: Optional[Path] = None) -> list[str]:
    path = Path(path) if path else resolve_proxies_file()
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = _clean(line)
        if is_comment(s):
            continue
        out.append(s)
    return out


def normalize_proxy(raw: str) -> str:
    """Приводит любую запись прокси к виду scheme://user:pass@host:port.

    Пусто и слова из DIRECT_WORDS (direct, none, -) значат «без прокси».
    """
    s = _clean(raw)
    if not s or s.lower() in DIRECT_WORDS:
        return ""
    s = s.replace(" ", "")
    if PROXY_SCHEME_RE.match(s):
        scheme, rest = s.split("://", 1)
        return f"{scheme.lower()}://{rest}"
    if "@" in s:
        return f"http://{s}"
    if s.count(":") == 3:
        host, port, user, pwd = s.split(":")
        return f"http://{user}:{pwd}@{host}:{port}"
    if s.count(":") == 1:
        return f"http://{s}"
    return f"http://{s}"


def proxy_host_port(raw: str) -> str:
    s = normalize_proxy(raw)
    if not s:
        return ""
    rest = s.split("://", 1)[1]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return rest


def proxy_to_playwright(raw: str) -> Optional[dict]:
    s = normalize_proxy(raw)
    if not s:
        return None
    scheme, rest = s.split("://", 1)
    username = password = ""
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    out = {"server": f"{scheme}://{rest}"}
    if username:
        out["username"] = username
    if password:
        out["password"] = password
    return out


def resolve_proxy(account: Account, pool: Optional[list[str]] = None) -> str:
    """Прокси аккаунта — только свой, из его строки в accounts.txt.

    Раздачи из proxies.txt по кругу больше нет: один аккаунт — один прокси.
    Пустая строка означает «прокси не закреплён» (аккаунт не в работе).
    """
    return normalize_proxy(account.proxy_raw) if (account.proxy_raw or "").strip() else ""


def proxy_sharing(accounts: list[Account], used: dict[str, list[str]]) -> dict[str, list[str]]:
    """Адреса, которые обслуживают больше одного аккаунта: {прокси: [логины]}."""
    by_proxy: dict[str, list[str]] = {}
    for acc in accounts:
        proxy = resolve_proxy(acc)
        if proxy:
            by_proxy.setdefault(proxy, []).append(acc.login)
    return {proxy: logins for proxy, logins in by_proxy.items() if len(logins) > 1}


if __name__ == "__main__":
    accounts = load_accounts()
    pool = proxies_from_file()
    print(f"аккаунтов: {len(accounts)}, прокси в пуле: {len(pool)}")
    for acc in accounts:
        print(
            f"  #{acc.line_no} {acc.login} "
            f"2fa={'да' if acc.twofa_secret else 'нет'} "
            f"proxy={proxy_host_port(resolve_proxy(acc, pool)) or '—'}"
        )
