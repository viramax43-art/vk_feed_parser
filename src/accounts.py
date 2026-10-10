#!/usr/bin/env python3
"""Аккаунты VK и прокси из data/accounts.txt.

Строка файла: телефон;пароль;2FA_СЕКРЕТ;прокси
Телефон — +7XXXXXXXXXX (или id/screen_name, если вход по логину). 2FA_СЕКРЕТ —
base32-секрет TOTP, можно оставить пустым. Прокси — host:port:user:pass,
user:pass@ip:port, http://user:pass@host:port или socks5://...; если не указан,
берётся строка proxies.txt с тем же номером, что у аккаунта, а слово direct (или
none, -) значит работу без прокси. Строки, начинающиеся с # или //, игнорируются.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from paths import ACCOUNTS_FILE, DATA_DIR, resolve_proxies_file

PROXY_SCHEME_RE = re.compile(r"^(https?|socks5h?|socks4)://", re.IGNORECASE)
DIRECT_WORDS = {"direct", "none", "no-proxy", "напрямую", "-"}  # «без прокси»


@dataclass
class Account:
    login: str
    password: str = ""
    twofa_secret: str = ""
    proxy_raw: str = ""
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


def parse_account_line(line: str, line_no: int = 0) -> Optional[Account]:
    s = _clean(line)
    if is_comment(s):
        return None
    parts = [p.strip() for p in s.split(";")]
    if len(parts) == 1:
        # Разрешаем «dump»-строку: телефон,пароль,2fa,прокси через запятую.
        parts = [p.strip() for p in s.split(",")]
    if len(parts) < 2:
        return None
    login = parts[0]
    if not login:
        return None
    return Account(
        login=login,
        password=parts[1] if len(parts) > 1 else "",
        twofa_secret=parts[2] if len(parts) > 2 else "",
        proxy_raw=parts[3] if len(parts) > 3 else "",
        line_no=line_no,
    )


def load_accounts(path: Path = ACCOUNTS_FILE) -> list[Account]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[Account] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        acc = parse_account_line(line, idx)
        if acc:
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
    """Прокси аккаунта: из строки, иначе из пула по номеру строки."""
    if (account.proxy_raw or "").strip():
        return normalize_proxy(account.proxy_raw)
    pool = pool if pool is not None else proxies_from_file()
    if not pool:
        return ""
    idx = max(0, (account.line_no or 1) - 1)
    return normalize_proxy(pool[idx % len(pool)])


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
