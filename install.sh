#!/usr/bin/env bash
# Instala/atualiza o WAN Monitor num LXC/VM Debian ou Ubuntu (correr como root, dentro da pasta wan-monitor)
set -euo pipefail
trap 'echo; echo "ERRO: a instalação parou na linha $LINENO: $BASH_COMMAND" >&2' ERR

APP_DIR=/opt/wan-monitor
ENV_FILE=/etc/wan-monitor.env
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Corre como root:  sudo bash install.sh  (ou entra como root no contentor)" >&2
  exit 1
fi

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
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -c "import flask, waitress, librouteros" \
  || { echo "ERRO: dependências Python em falta (sem internet no contentor?)" >&2; exit 1; }
chown -R wanmon:wanmon "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
  install -m 640 "$SRC/wan-monitor.env.example" "$ENV_FILE"
  echo "==> Criado $ENV_FILE  (EDITA-O com os dados do teu MikroTik)"
fi
# remove finais de linha do Windows (\r) se o ficheiro foi editado noutro sistema
sed -i 's/\r$//' "$ENV_FILE"
chown root:wanmon "$ENV_FILE"; chmod 640 "$ENV_FILE"

echo "==> Serviço systemd"
install -m 644 "$SRC/wan-monitor.service" /etc/systemd/system/wan-monitor.service
systemctl daemon-reload
systemctl enable wan-monitor >/dev/null 2>&1
systemctl restart wan-monitor

sleep 4
IP=$(hostname -I | awk '{print $1}')
PORT=$(grep -E '^LISTEN_PORT=' "$ENV_FILE" | cut -d= -f2 | tr -d ' ' || true)
echo
if systemctl is-active --quiet wan-monitor; then
  echo "Pronto!  Abre:  http://$IP:${PORT:-8080}"
else
  echo "ATENÇÃO: o serviço não ficou a correr. Últimas linhas do registo:" >&2
  journalctl -u wan-monitor -n 25 --no-pager >&2 || true
fi
echo "Logs:          journalctl -u wan-monitor -f"
echo "Config:        nano $ENV_FILE && systemctl restart wan-monitor"
