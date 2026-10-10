#!/usr/bin/env python3
"""Проверка прокси: жив ли, какой IP отдаёт, пускает ли на VK.

Запуск:
    python src/check_proxy.py                          # все прокси из proxies.txt
    python src/check_proxy.py --account 1              # прокси аккаунта №1
    python src/check_proxy.py --json                   # машинный вывод
    python src/check_proxy.py --raw --account 1        # сокетная диагностика
    python src/check_proxy.py --browser --account 1    # проверка реальным Chromium

Проверка: api.ipify.org (внешний IP и задержка), api.vk.com и vk.com (доступен ли
VK через этот прокси). Код выхода 1, если хоть один прокси мёртв.

Режимы: --raw — TCP, CONNECT и обычный HTTP на уровне сокета, видно, отвечает ли
прокси вообще; --browser — реальный Chromium через прокси (то же, что делает вход в
VK); direct (или none/-) вместо адреса — контрольная проверка без прокси.

Скрипт предупреждает, если в Windows включён системный прокси: трафик идёт через
VPN/TUN, и RU-прокси с авторизацией по IP молча дропают запросы (TCP открывается в
туннеле, а данных нет) — выключите VPN и повторите проверку.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts import (  # noqa: E402
    load_accounts,
    normalize_proxy,
    proxy_host_port,
    proxies_from_file,
    resolve_proxy,
)

IP_URL = "https://api.ipify.org?format=json"
VK_API_URL = "https://api.vk.com/method/users.get?v=5.199"
VK_WEB_URL = "https://vk.com/"

# Псевдонимы «без прокси» — для контрольной проверки прямого соединения.
DIRECT_ALIASES = {"direct", "none", "no-proxy", "напрямую", "-"}


def target_label(raw: str) -> str:
    """Подпись прокси для отчёта; пустая строка = прямое соединение."""
    proxy = normalize_proxy(raw)
    if not proxy:
        return "direct (без прокси)"
    return proxy_host_port(proxy) or raw


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def system_proxy_state() -> str:
    """Адрес системного прокси Windows (VPN/TUN): влияет на Chromium и curl."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
            try:
                auto = winreg.QueryValueEx(key, "AutoConfigURL")[0]
            except OSError:
                auto = ""
    except OSError:
        return ""
    if enabled and server:
        return str(server)
    return str(auto or "")


def _body(resp) -> dict:
    try:
        return json.loads(resp.text or "{}")
    except Exception:  # noqa: BLE001
        return {}


def check_proxy(raw: str, timeout: float = 30.0) -> dict:
    """Возвращает словарь-отчёт по одному прокси."""
    from vk_auth import _http_session  # локальный импорт: не тянем playwright при --help

    proxy = normalize_proxy(raw)
    report = {
        "proxy": target_label(raw),
        "url": proxy,
        "ok": False,
        "ip": "",
        "latency": None,
        "vk_api": "",
        "vk_web": "",
        "error": "",
    }
    try:
        session = _http_session(proxy)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"клиент не создан ({exc})"
        return report

    started = time.time()
    try:
        resp = session.get(IP_URL, timeout=timeout)
        report["latency"] = round(time.time() - started, 2)
        if resp.status_code != 200:
            report["error"] = f"ipify вернул HTTP {resp.status_code}"
            return report
        report["ip"] = str(_body(resp).get("ip") or "").strip()
        report["ok"] = bool(report["ip"])
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"нет связи ({exc})"[:200]
        return report

    try:
        resp = session.get(VK_API_URL, timeout=timeout)
        body = _body(resp)
        err = body.get("error") or {}
        if err:
            report["vk_api"] = (
                f"HTTP {resp.status_code}, ошибка API {err.get('error_code')}: "
                f"{str(err.get('error_msg') or '').splitlines()[0][:60]}"
            )
        elif body.get("response") is not None:
            report["vk_api"] = "доступен"
        else:
            report["vk_api"] = f"HTTP {resp.status_code}, пустой ответ"
    except Exception as exc:  # noqa: BLE001
        report["vk_api"] = f"нет связи ({exc})"[:120]

    try:
        resp = session.get(VK_WEB_URL, timeout=timeout)
        report["vk_web"] = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        report["vk_web"] = f"нет связи ({exc})"[:120]

    return report


def raw_probe(raw: str, timeout: float = 15.0, target: str = "api.vk.com:443") -> dict:
    """Сокетная проверка: открыт ли порт и отвечает ли прокси на запросы."""
    import base64
    import socket

    proxy = normalize_proxy(raw)
    hostport = proxy_host_port(proxy)
    host, _, port = hostport.partition(":")
    report = {
        "proxy": target_label(raw),
        "tcp": False,
        "connect": "",
        "http": "",
        "error": "",
    }
    if not host or not port.isdigit():
        report["error"] = (
            "для --raw нужен адрес прокси (host:port)" if not proxy else f"не разобрал адрес прокси: {raw!r}"
        )
        return report
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        report["tcp"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"TCP не открылся ({exc})"[:160]
        return report

    rest = proxy.split("://", 1)[-1]
    creds = rest.rsplit("@", 1)[0] if "@" in rest else ""
    auth = ""
    if creds:
        token = base64.b64encode(creds.encode()).decode()
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    probes = {
        "connect": f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n",
        "connect_noauth": f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n",
        "http": f"GET http://api.ipify.org/ HTTP/1.1\r\nHost: api.ipify.org\r\n{auth}\r\n",
    }
    for key, request in probes.items():
        report[key] = _send_probe(host, int(port), request, timeout)
    # Некоторые панели отдают HTTPS-прокси: тогда на порт нужен TLS-запрос.
    report["tls"] = _tls_probe(host, int(port), timeout)
    return report


def _send_probe(host: str, port: int, request: str, timeout: float) -> str:
    """Отправляет HTTP-запрос в сокет и возвращает первую строку ответа."""
    import socket

    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(request.encode())
            first = sock.recv(160)
        return f"{first.splitlines()[0].decode('latin-1', 'replace')} за {round(time.time() - started, 1)} c"
    except Exception as exc:  # noqa: BLE001
        return f"нет ответа за {round(time.time() - started, 1)} c ({str(exc)[:70]})"


def _tls_probe(host: str, port: int, timeout: float) -> str:
    """Шлёт настоящий TLS ClientHello: видно, отвечает ли порт (и чем) на TLS."""
    import socket
    import ssl

    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        handshake = context.wrap_bio(incoming, outgoing, server_hostname=host)
        try:
            handshake.do_handshake()
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        hello = outgoing.read()
    except Exception as exc:  # noqa: BLE001
        return f"не собрал ClientHello ({str(exc)[:60]})"

    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(hello)
            data = sock.recv(160)
        return f"ответ {data[:90]!r} за {round(time.time() - started, 1)} c"
    except Exception as exc:  # noqa: BLE001
        return f"нет ответа за {round(time.time() - started, 1)} c ({str(exc)[:70]})"


def raw_ok(report: dict) -> bool:
    answers = (report.get("connect", ""), report.get("http", ""), report.get("tls", ""))
    return bool(report["tcp"] and any(a.startswith("HTTP/1.") for a in answers))


def print_raw_report(report: dict, index: int = 0, total: int = 0) -> None:
    prefix = f"[{index}/{total}] " if total else ""
    head = f"{prefix}{report['proxy']}"
    if not report["tcp"]:
        log(f"{head}: {report['error'] or 'TCP не открылся'}")
        return
    verdict = "" if raw_ok(report) else (
        "  <-- порт открыт, но прокси молчит: VPN/TUN перехватывает трафик, "
        "IP не в белом списке панели либо блокировка провайдера"
    )
    log(
        f"{head}: TCP открыт | CONNECT: {report['connect']} | "
        f"HTTP: {report['http']} | без авторизации: {report.get('connect_noauth', '—')} | "
        f"TLS: {report.get('tls', '—')}{verdict}"
    )


def browser_probe(raw: str, url: str = IP_URL, timeout: float = 40.0) -> dict:
    """Открывает настоящий Chromium через прокси — как при входе в VK."""
    proxy = normalize_proxy(raw)
    report = {
        "proxy": target_label(raw),
        "ok": False,
        "status": 0,
        "body": "",
        "error": "",
    }
    try:
        from playwright.sync_api import sync_playwright

        from accounts import proxy_to_playwright
    except ImportError as exc:  # pragma: no cover
        report["error"] = f"нужен playwright: {exc}"
        return report

    launch: dict = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    pw_proxy = proxy_to_playwright(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch)
            try:
                page = browser.new_page()
                page.set_default_timeout(int(timeout * 1000))
                resp = page.goto(url, wait_until="domcontentloaded")
                report["status"] = int(resp.status) if resp else 0
                report["ok"] = bool(resp and resp.status < 400)
                report["body"] = (page.inner_text("body") or "").strip().replace("\n", " ")[:120]
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc).splitlines()[0][:200]
    return report


def print_browser_report(report: dict, index: int = 0, total: int = 0) -> None:
    prefix = f"[{index}/{total}] " if total else ""
    head = f"{prefix}{report['proxy']}"
    if not report["ok"]:
        log(f"{head}: браузер не прошёл — HTTP {report['status']} {report['error']}".rstrip())
        return
    log(f"{head}: браузер ок, HTTP {report['status']}, ответ: {report['body']!r}")


def load_targets(args) -> list[str]:
    """Список прокси для проверки: --proxy, аккаунт или пул из proxies.txt."""
    if args.proxy:
        return ["" if p.strip().lower() in DIRECT_ALIASES else p for p in args.proxy]
    if args.account:
        accounts = load_accounts()
        if not accounts:
            raise SystemExit("нет аккаунтов в data/accounts.txt")
        idx = max(1, args.account) - 1
        return [resolve_proxy(accounts[min(idx, len(accounts) - 1)])]
    return proxies_from_file()


def print_report(report: dict, index: int = 0, total: int = 0) -> None:
    prefix = f"[{index}/{total}] " if total else ""
    head = f"{prefix}{report['proxy']}"
    if not report["ok"]:
        log(f"{head}: НЕ РАБОТАЕТ — {report['error'] or 'неизвестная ошибка'}")
        return
    latency = report["latency"] if report["latency"] is not None else "?"
    log(
        f"{head}: ok, IP {report['ip']}, {latency} c | "
        f"api.vk.com: {report['vk_api'] or '—'} | vk.com: {report['vk_web'] or '—'}"
        f"{' | ВНИМАНИЕ: VK не отвечает' if 'нет связи' in report['vk_api'] + report['vk_web'] else ''}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка прокси для VK")
    parser.add_argument("--proxy", action="append", default=[], help="прокси (можно несколько раз)")
    parser.add_argument("--account", type=int, default=0, help="взять прокси аккаунта из accounts.txt")
    parser.add_argument("--timeout", type=float, default=30.0, help="таймаут запроса, сек")
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--raw", action="store_true", help="сокетная диагностика (TCP/CONNECT/HTTP)")
    mode.add_argument("--browser", action="store_true", help="проверка настоящим Chromium")
    mode.add_argument("--vk", action="store_true", help="(по умолчанию) http-проверка + VK API")
    args = parser.parse_args(argv)

    targets = load_targets(args)
    if not args.json:
        sys_proxy = system_proxy_state()
        if sys_proxy:
            log(
                f"внимание: в Windows включён системный прокси {sys_proxy} — "
                "трафик (в т.ч. Chromium и curl) идёт через VPN/TUN; "
                "RU-прокси с авторизацией по IP могут молча дропать такие запросы"
            )
    if not targets:
        log("нечего проверять: proxies.txt пуст (или передайте --proxy)")
        return 1
    reports: list[dict] = []
    for idx, raw in enumerate(targets, start=1):
        if args.raw:
            report = raw_probe(raw, timeout=min(args.timeout, 20.0))
            reports.append(report)
            if not args.json:
                print_raw_report(report, idx, len(targets))
            continue
        if args.browser:
            report = browser_probe(raw, timeout=max(args.timeout, 40.0))
            reports.append(report)
            if not args.json:
                print_browser_report(report, idx, len(targets))
            continue
        report = check_proxy(raw, timeout=args.timeout)
        reports.append(report)
        if not args.json:
            print_report(report, idx, len(targets))

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))

    alive = [r for r in reports if (raw_ok(r) if args.raw else r["ok"])]
    if not args.json:
        log(f"итого: рабочих {len(alive)} из {len(reports)}")
    return 0 if len(alive) == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
