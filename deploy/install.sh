#!/usr/bin/env bash
# Install the gateway on a Linux host (Raspberry Pi, mini-PC).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo ./deploy/install.sh" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="/opt/sim7600-gateway"
CONF_DIR="/etc/sim7600-gateway"
STATE_DIR="/var/lib/sim7600-gateway"

command -v python3 >/dev/null || { echo "Нужен python3 (>= 3.11)"; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Нужен python3 >= 3.11"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 \
  || { echo "Нужен пакет python3-venv (apt install python3-venv)"; exit 1; }
command -v ffmpeg >/dev/null || echo "Внимание: ffmpeg не найден — камера не заработает"
command -v v4l2-ctl >/dev/null || echo "Внимание: v4l-utils не установлен — автоопределение камеры ограничено"
if ! command -v arecord >/dev/null || ! command -v aplay >/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get update && apt-get install -y --no-install-recommends alsa-utils
  else
    echo "Внимание: arecord/aplay не найдены (alsa-utils) — голосовые звонки не будут работать"
  fi
fi

if ! id sim7600 >/dev/null 2>&1; then
  useradd --system --home "$DEST" --shell /usr/sbin/nologin sim7600
fi
usermod -aG dialout,video,audio sim7600 2>/dev/null || true

install -d "$DEST" "$CONF_DIR" "$STATE_DIR"
cp -r "$SRC/sim7600_gateway" "$DEST/"
cp "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/requirements.txt" "$DEST/"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --upgrade pip >/dev/null
"$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"

if [ ! -f "$CONF_DIR/config.toml" ]; then
  cp "$SRC/config.example.toml" "$CONF_DIR/config.toml"
  sed -i 's#state_dir = "./data"#state_dir = "/var/lib/sim7600-gateway"#' "$CONF_DIR/config.toml"
  sed -i 's#snapshot_dir = "./data/snapshots"#snapshot_dir = "/var/lib/sim7600-gateway/snapshots"#' "$CONF_DIR/config.toml"
  API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed -i "s|api_token = \"change-me-please\"|api_token = \"$API_TOKEN\"|" "$CONF_DIR/config.toml"
  echo "Создан $CONF_DIR/config.toml — отредактируйте URL/токен Athena."
  echo "Сгенерирован API-токен: $API_TOKEN"
  echo "Впишите его в Home Assistant (интеграция Athena SIM7600)."
fi

chown -R sim7600:sim7600 "$STATE_DIR"

install -m 0644 "$SRC/deploy/sim7600-gateway.service" /etc/systemd/system/sim7600-gateway.service
install -m 0644 "$SRC/deploy/99-sim7600.rules" /etc/udev/rules.d/99-sim7600.rules
udevadm control --reload-rules
udevadm trigger || true
systemctl daemon-reload
chown -R sim7600:sim7600 "$STATE_DIR"
systemctl enable --now sim7600-gateway.service

echo
echo "Готово. Статус: systemctl status sim7600-gateway"
echo "Логи:        journalctl -u sim7600-gateway -f"
echo "Проверка:    curl http://127.0.0.1:8099/api/health"
