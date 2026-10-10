#!/bin/bash
# Двойной клик по этому файлу открывает панель VK Feed Parser в браузере.
cd "$(dirname "$0")" || exit 1
printf '\n=== VK Feed Parser: панель в браузере ===\n\n'

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python не найден."
  echo "Поставьте его с https://www.python.org/downloads/ и запустите файл снова."
  echo
  read -r -p "Enter — закрыть…"
  exit 1
fi

"$PY" start_panel.py "$@"
echo
read -r -p "Панель остановлена. Enter — закрыть это окно…" || true
exit 0
