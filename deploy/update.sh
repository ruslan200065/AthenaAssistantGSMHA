#!/usr/bin/env bash
# Update an installed gateway to the current release from this repo copy.
# Config in /etc/sim7600-gateway is left untouched; the venv is rebuilt so new
# dependencies arrive too.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo ./deploy/update.sh" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="/opt/sim7600-gateway"
CONF_DIR="/etc/sim7600-gateway"
STATE_DIR="/var/lib/sim7600-gateway"

if [ ! -d "$DEST" ]; then
  echo "Шлюз не установлен в $DEST — используйте sudo ./deploy/install.sh" >&2
  exit 1
fi

NEW_VERSION="$(sed -n 's/.*__version__ = "\([^"]*\)".*/\1/p' "$SRC/sim7600_gateway/__init__.py" | head -n1)"
echo "Обновляю шлюз до версии $NEW_VERSION"

systemctl stop sim7600-gateway.service || true

rm -rf "$DEST/sim7600_gateway"
cp -r "$SRC/sim7600_gateway" "$DEST/"
cp "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/requirements.txt" "$DEST/"

if [ ! -x "$DEST/venv/bin/pip" ]; then
  python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install --upgrade pip >/dev/null
"$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"

install -m 0644 "$SRC/deploy/sim7600-gateway.service" /etc/systemd/system/sim7600-gateway.service
install -m 0644 "$SRC/deploy/99-sim7600.rules" /etc/udev/rules.d/99-sim7600.rules
udevadm control --reload-rules
udevadm trigger || true
systemctl daemon-reload

chown -R sim7600:sim7600 "$DEST" "$STATE_DIR"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} + || true

systemctl start sim7600-gateway.service
sleep 2
systemctl --no-pager --lines=5 status sim7600-gateway.service || true

echo
echo "Проверка версии: curl -s http://127.0.0.1:8099/api/health"
echo "Ожидаемо: {\"status\":\"ok\",\"version\":\"$NEW_VERSION\",...}"
