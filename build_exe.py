#!/usr/bin/env python3
"""Build VK Feed Parser.exe with PyInstaller.

The window is opened by Edge/Chrome in app mode, so the build needs NO pywebview,
pythonnet, clr_loader or .NET: those packages are excluded and the
"Failed to resolve Python.Runtime.Loader.initialize" failure cannot happen.

Important: src/ is both added as data (the panel runs its modules via runpy) AND
registered as a module path. Without --paths src PyInstaller never reads the
imports inside src/webui.py, so stdlib modules like http.server are missing and
the exe dies with "No module named http.server".

Usage (Windows, from the project folder):
    python build_exe.py            windowed exe
    python build_exe.py --console  diagnostic exe with a visible console
    python build_exe.py --no-check skip the post-build self-check

Result: dist/VK Feed Parser/VK Feed Parser.exe
Build on Windows for a Windows exe.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "VK Feed Parser"
SEP = ";" if sys.platform.startswith("win") else ":"

# Модули проекта: их PyInstaller должен проанализировать, чтобы увидеть импорты
# (http.server, socket, urllib, sqlite3 …), которые есть только внутри src/.
PROJECT_MODULES = [
    "webui", "run_tags", "vk_auth", "account_pool", "check_proxy", "export_xlsx",
    "probe", "settings", "gui", "accounts", "paths", "state_db", "vk_client",
    "platform_compat",
]


def run(cmd: list[str], *, check: bool = True) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check).returncode


def remove_obsolete_pathlib() -> None:
    run([sys.executable, "-m", "pip", "uninstall", "-y", "pathlib"], check=False)


def self_check(exe: Path, out_dir: Path) -> int:
    """Запускает exe с --selfcheck: панель должна импортироваться без окна и сети."""
    print()
    print("Проверяю сборку (--selfcheck)…")
    log = out_dir / "data" / "logs" / "startup.log"
    before = log.stat().st_size if log.exists() else 0
    proc = subprocess.run([str(exe), "--selfcheck"], cwd=out_dir,
                          capture_output=True, text=True, timeout=180)
    tail = ""
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        tail = text[before:]
    print(f"--selfcheck: код {proc.returncode}")
    for part in (proc.stdout, proc.stderr, tail):
        if part and part.strip():
            print(part.strip()[-1500:])
    if proc.returncode != 0:
        print("Сборка не запускается — исправьте ошибку выше и пересоберите.")
    return proc.returncode


def main() -> int:
    if not (ROOT / "desktop_app.py").exists():
        print("desktop_app.py not found next to build_exe.py")
        return 1

    remove_obsolete_pathlib()
    run([sys.executable, "-m", "pip", "install", "-q", "--upgrade",
         "pyinstaller", "-r", "requirements.txt"])

    for old in ("build", "dist"):
        shutil.rmtree(ROOT / old, ignore_errors=True)

    console = "--console" in sys.argv
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--console" if console else "--windowed",
        "--name", APP_NAME,
        "--paths", "src",                  # чтобы PyInstaller читал импорты src/*
        "--add-data", f"src{SEP}src",      # сами исходники (панель запускает их как файлы)
        "--hidden-import", "http.server",  # панель поднимает HTTP-сервер
        "--hidden-import", "socketserver",
        "--hidden-import", "urllib.parse",
        "--hidden-import", "urllib.request",
        "--hidden-import", "curl_cffi",
        "--hidden-import", "webbrowser",
        "--hidden-import", "pyotp",
        "--hidden-import", "xlsxwriter",
        "--hidden-import", "playwright.sync_api",
        "--hidden-import", "sqlite3",
        # Окно рисует Edge: эти пакеты не нужны и только ломают сборку.
        "--exclude-module", "tkinter",
        "--exclude-module", "webview",
        "--exclude-module", "pythonnet",
        "--exclude-module", "clr_loader",
    ]
    for mod in PROJECT_MODULES:
        cmd += ["--hidden-import", mod]
    cmd.append("desktop_app.py")
    run(cmd)

    out = ROOT / "dist" / APP_NAME
    exe = out / f"{APP_NAME}.exe"
    if not exe.exists() and not (out / APP_NAME).exists():
        print("build finished but the executable was not created")
        return 1
    if not exe.exists():                      # сборка на Linux/macOS: без .exe
        exe = out / APP_NAME
    (out / "data").mkdir(exist_ok=True)
    print()
    print(f"Done: {exe}")
    print("Keep the data\\ folder when updating: it holds accounts and sessions.")

    if "--no-check" in sys.argv:
        return 0
    return self_check(exe, out)


if __name__ == "__main__":
    raise SystemExit(main())
