#!/bin/bash
# Запуск панели VK Feed Parser на Linux (сервер, VPS, десктоп).
#
#   chmod +x start_panel.sh      # один раз: сделать файл исполняемым
#   ./start_panel.sh             # поднять панель (первый раз поставит библиотеки)
#   ./start_panel.sh --port 9000
#   ./start_panel.sh --host 0.0.0.0   # панель доступна по сети (у неё НЕТ пароля)
#   ./start_panel.sh --no-browser     # не пытаться открыть браузер (сервер, ssh)
#
# Скрипт только находит python3 и передаёт работу start_panel.py: он сам создаст
# .venv, поставит зависимости и Chromium. Если графики нет, панель всё равно
# поднимется — адрес печатается в окне, а браузер открывается на своей машине
# через SSH-туннель:  ssh -L 8787:127.0.0.1:8787 user@сервер
cd "$(dirname "$0")" || exit 1
printf '\n=== VK Feed Parser: панель в браузере (Linux) ===\n\n'

PY="$(command -v python3 || command -v python3.12 || command -v python3.11 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3 не найден. Поставьте его:"
  echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  echo
  read -r -p "Enter — закрыть…" || true
  exit 1
fi

"$PY" start_panel.py "$@"
code=$?
echo
read -r -p "Панель остановлена (код $code). Enter — закрыть это окно…" || true
exit "$code"
