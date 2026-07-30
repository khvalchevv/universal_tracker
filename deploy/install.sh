#!/usr/bin/env bash
# Розгортання трекера на Linux-сервері з systemd.
#
#   sudo bash deploy/install.sh
#
# Ідемпотентний: можна ганяти повторно для оновлення.
set -euo pipefail

APP_DIR=/opt/spread-tracker
ENV_FILE=/etc/spread-tracker.env
SERVICE=spread-tracker
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Потрібен root: sudo bash deploy/install.sh"; exit 1; }

command -v python3 >/dev/null || { echo "Немає python3"; exit 1; }
python3 - <<'PY' || { echo "Потрібен Python 3.9+"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

id -u tracker &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin tracker

install -d -o tracker -g tracker -m 750 "$APP_DIR"
install -o tracker -g tracker -m 640 "$SRC/spread_tracker.py" "$APP_DIR/"
install -o tracker -g tracker -m 640 "$SRC/test_tracker.py"   "$APP_DIR/"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# Токен від @BotFather і твій chat id.
TG_BOT_TOKEN=
TG_CHAT_ID=
EOF
  chown root:tracker "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo
  echo ">>> Впиши ключі у $ENV_FILE, потім:"
  echo ">>>   systemctl start $SERVICE"
fi

install -m 644 "$SRC/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

# Перевіряємо, що код узагалі робочий, перш ніж пускати в бій.
# Вивід тестів ховаємо лише коли все добре — при збої він потрібен на екрані.
echo "Python: $(python3 --version 2>&1)"
if out=$(sudo -u tracker python3 "$APP_DIR/test_tracker.py" 2>&1); then
  echo "Тести: $(printf '%s\n' "$out" | tail -1)"
else
  echo
  echo "!!! Тести не пройшли — сервіс не запускаю."
  printf '%s\n' "$out" | tail -25
  exit 1
fi

if grep -q '^TG_BOT_TOKEN=.\+' "$ENV_FILE"; then
  systemctl restart "$SERVICE"
  sleep 3
  systemctl --no-pager --lines=15 status "$SERVICE" || true
fi

echo
echo "Готово. Далі:"
echo "  journalctl -u $SERVICE -f      # живий лог"
echo "  systemctl restart $SERVICE     # перезапуск"
