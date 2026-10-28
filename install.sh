#!/usr/bin/env bash
# Instala o WAN Monitor num LXC/VM Debian ou Ubuntu (correr como root, dentro da pasta wan-monitor)
set -euo pipefail

APP_DIR=/opt/wan-monitor
ENV_FILE=/etc/wan-monitor.env
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Pacotes"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

echo "==> Utilizador de serviço"
id wanmon &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin wanmon

echo "==> Ficheiros"
mkdir -p "$APP_DIR/static"
install -m 644 "$SRC/app.py" "$APP_DIR/app.py"
install -m 644 "$SRC/requirements.txt" "$APP_DIR/requirements.txt"
install -m 644 "$SRC/static/index.html" "$APP_DIR/static/index.html"

echo "==> Ambiente Python"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R wanmon:wanmon "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 "$SRC/wan-monitor.env.example" "$ENV_FILE"
  chown root:wanmon "$ENV_FILE"; chmod 640 "$ENV_FILE"
  echo "==> Criado $ENV_FILE  (EDITA-O com os dados do teu MikroTik)"
fi

echo "==> Serviço systemd"
install -m 644 "$SRC/wan-monitor.service" /etc/systemd/system/wan-monitor.service
systemctl daemon-reload
systemctl enable --now wan-monitor
systemctl restart wan-monitor

IP=$(hostname -I | awk '{print $1}')
PORT=$(grep -E '^LISTEN_PORT=' "$ENV_FILE" | cut -d= -f2 || echo 8080)
echo
echo "Pronto!  Abre:  http://$IP:${PORT:-8080}"
echo "Logs:          journalctl -u wan-monitor -f"
echo "Config:        nano $ENV_FILE && systemctl restart wan-monitor"
