#!/usr/bin/env python3
"""Проверка открытия страницы входа VK: пустое окно (about:blank) больше не ломает вход.

    python tests/test_login_page.py

Интернет не нужен: вместо vk.ru поднимается локальный HTTP-сервер, вместо прокси —
мёртвый порт и «зависший» прокси (сначала молчит, потом начинает работать).
Проверяем, что вход не падает с трейсбеком, когда прокси не отвечает: причина сбоя
(Timeout … domcontentloaded / ERR_PROXY_CONNECTION_FAILED) попадает в лог вместе с
понятным «окно осталось на about:blank», а после оживления прокси страница
открывается со второй попытки. Браузер берётся тот же, что и во входе (Edge, иначе
Chrome); если ни одного нет — проверка пропускается.
"""
from __future__ import annotations

import http.server
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import vk_auth  # noqa: E402
from accounts import proxy_to_playwright  # noqa: E402

FAILED: list[str] = []
SKIPPED = "браузер не запустился"
ATTEMPT_MS = 4000  # короткие таймауты, чтобы проверка не длилась минутами


def check(cond: bool, msg: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILED.append(msg)



# --- заглушки сети: страница входа и «плохие» прокси ---
class LoginPage(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = "<html><body><form>вход</form></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102
        pass


class QuietServer(http.server.HTTPServer):
    """Обрыв соединения браузером в конце проверки — это норма, не печатаем трейсбек."""

    def handle_error(self, request, client_address):  # noqa: D102
        pass


def start_login_page() -> tuple[str, int, http.server.HTTPServer]:
    srv = QuietServer(("127.0.0.1", 0), LoginPage)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    return f"http://{host}:{port}/", port, srv


def pipe(src: socket.socket, dst: socket.socket) -> None:
    """Переливает байты из src в dst, пока клиент не закроет соединение."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def start_flaky_proxy(target_port: int, ready_after: float) -> tuple[str, socket.socket]:
    """Прокси «сначала мёртвый, потом живой»: до ready_after принимает и молчит."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    host, port = srv.getsockname()

    def handle(client: socket.socket) -> None:
        remote: socket.socket | None = None
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request += chunk
            if time.time() < ready_after:
                # туннель висит: не отвечаем — клиент упирается в Timeout
                while time.time() < ready_after:
                    time.sleep(0.2)
                return
            head, _, rest = request.partition(b"\r\n\r\n")
            line, *headers = head.split(b"\r\n")
            method, url, version = line.split(b" ", 2)
            if b"://" in url:  # absolute-form → origin-form
                url = b"/" + url.split(b"://", 1)[1].split(b"/", 1)[-1]
            remote = socket.create_connection(("127.0.0.1", target_port), timeout=10)
            remote.sendall(
                b" ".join([method, url, version])
                + b"\r\n"
                + b"\r\n".join(headers)
                + b"\r\n\r\n"
                + rest
            )
            threading.Thread(target=pipe, args=(remote, client), daemon=True).start()
            pipe(client, remote)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv


def with_page(profile: Path, proxy: str, run) -> bool:
    """Открывает окно входа (как vk_auth) и отдаёт его в run. False — браузера нет."""
    from playwright.sync_api import sync_playwright

    profile.mkdir(parents=True, exist_ok=True)
    launch = {
        "headless": True,
        "locale": "ru-RU",
        "viewport": {"width": 1366, "height": 900},
        "args": [
            "--disable-blink-features=AutomationControlled",
            # иначе Chromium ходит в 127.0.0.1 мимо прокси и проверка теряет смысл
            "--proxy-bypass-list=<-loopback>",
        ],
    }
    pw_proxy = proxy_to_playwright(proxy)
    if pw_proxy:
        launch["proxy"] = pw_proxy
    with sync_playwright() as pw:
        try:
            context = vk_auth._launch_context(pw, profile, launch)
        except Exception as exc:  # noqa: BLE001
            print(f"пропуск: {SKIPPED} ({str(exc).splitlines()[0][:120]})")
            return False
        try:
            page = context.pages[0] if context.pages else context.new_page()
            run(page)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    return True


def capture_log(run) -> list[str]:
    """Собирает строки, которые вход пишет через vk_auth.log."""
    lines: list[str] = []
    original = vk_auth.log
    vk_auth.log = lambda msg: lines.append(str(msg))
    try:
        run()
    finally:
        vk_auth.log = original
    return lines


tmp = Path(tempfile.mkdtemp(prefix="login_page_"))
page_url, target_port, page_srv = start_login_page()
tried_any = False

# 1. обычная страница входа открывается, окно не пустое
ok_result: dict = {}


def run_ok(page) -> None:
    ok, error = vk_auth._goto_login(page, page_url, ATTEMPT_MS)
    ok_result.update(ok=ok, error=error, blank=vk_auth._page_is_blank(page), url=page.url)


tried_any |= with_page(tmp / "ok", "", run_ok)
if ok_result:
    check(ok_result["ok"], f"страница входа открывается: {ok_result['url']!r}")
    check(not ok_result["blank"], "после удачного открытия окно не пустое")

# 2. мёртвый прокси: причина понятна, окно осталось на about:blank, без исключения
dead: dict = {}


def run_dead(page) -> None:
    ok, error = vk_auth._goto_login(page, page_url, ATTEMPT_MS)
    dead.update(ok=ok, error=error, blank=vk_auth._page_is_blank(page), url=page.url)


tried_any |= with_page(tmp / "dead", "http://127.0.0.1:9", run_dead)
if dead:
    check(not dead["ok"], "мёртвый прокси: страница входа не открылась (ошибки наружу нет)")
    check(
        dead["blank"],
        f"мёртвый прокси: окно пустое (Chromium рисует свой экран ошибки): {dead['url']!r}",
    )
    check(
        "ERR_PROXY_CONNECTION_FAILED" in dead["error"] or "Timeout" in dead["error"],
        f"в причине видно, что виноват прокси: {dead['error'][:110]!r}",
    )

# 3. «зависший» прокси: Timeout на domcontentloaded + пустое окно
flaky_url, flaky_srv = start_flaky_proxy(target_port, time.time() + 999)
stalled: dict = {}


def run_stalled(page) -> None:
    ok, error = vk_auth._goto_login(page, page_url, ATTEMPT_MS)
    stalled.update(ok=ok, error=error, blank=vk_auth._page_is_blank(page))


tried_any |= with_page(tmp / "stalled", flaky_url, run_stalled)
if stalled:
    check(not stalled["ok"], "зависший прокси: страница входа не открылась")
    check(stalled["blank"], "зависший прокси: окно осталось на about:blank")
    check(
        "Timeout" in stalled["error"] and "domcontentloaded" in stalled["error"],
        f"в причине видна ошибка domcontentloaded: {stalled['error'][:110]!r}",
    )
flaky_srv.close()

# 4. _open_login_page: несколько попыток, понятный текст, исключений нет
attempts: dict = {}


def run_attempts(page) -> None:
    def caller() -> None:
        ok, error = vk_auth._open_login_page(page, page_url, ATTEMPT_MS)
        attempts.update(ok=ok, error=error)

    attempts["lines"] = capture_log(caller)


tried_any |= with_page(tmp / "attempts", "http://127.0.0.1:9", run_attempts)
if attempts:
    tries = [line for line in attempts["lines"] if "попытка" in line]
    check(not attempts["ok"], "повторные попытки не выдумывают успех")
    check(
        len(tries) == vk_auth.LOGIN_PAGE_ATTEMPTS,
        f"попыток ровно {vk_auth.LOGIN_PAGE_ATTEMPTS}: {len(tries)}",
    )
    check(attempts["error"] != "", f"причина сбоя передана наружу: {attempts['error'][:110]!r}")

# 5. прокси ожил: страница входа открывается со второй попытки
recovered: dict = {}
alive_url, alive_srv = start_flaky_proxy(
    target_port, time.time() + (ATTEMPT_MS * 2) / 1000 + 2
)


def run_recovered(page) -> None:
    def caller() -> None:
        ok, error = vk_auth._open_login_page(page, page_url, ATTEMPT_MS)
        recovered.update(
            ok=ok, error=error, url=page.url, blank=vk_auth._page_is_blank(page)
        )

    recovered["lines"] = capture_log(caller)


tried_any |= with_page(tmp / "recovered", alive_url, run_recovered)
alive_srv.close()
if recovered:
    check(recovered["ok"], f"после оживления прокси вход открылся: {recovered.get('url')!r}")
    check(not recovered["blank"], "окно не пустое — человек может вводить пароль")
    check(
        any("с попытки 2" in line for line in recovered["lines"]),
        "в логе видно, что помогла вторая попытка",
    )

page_srv.shutdown()
page_srv.server_close()
shutil.rmtree(tmp, ignore_errors=True)
print("-" * 50)
if not tried_any:
    print(f"ПРОПУЩЕНО: {SKIPPED}")
    raise SystemExit(0)
print("ПРОВАЛЕНО: " + "; ".join(FAILED) if FAILED else "СТРАНИЦА ВХОДА ОТКРЫВАЕТСЯ")
raise SystemExit(1 if FAILED else 0)
