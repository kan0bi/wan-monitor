#!/usr/bin/env python3
"""
WAN Monitor - mostra o tráfego de duas WAN de um MikroTik em tempo real.
Lê os contadores rx-byte / tx-byte das interfaces via API RouterOS (porta 8728/8729)
usando um utilizador apenas de leitura, e calcula as taxas no servidor.
"""
import base64
import logging
import math
import os
import random
import ssl
import threading
import time
from collections import deque

from flask import Flask, Response, jsonify, request, send_from_directory

# ---------------------------------------------------------------- configuração
def env(key, default=None):
    return os.environ.get(key, default)

MT_HOST = env("MT_HOST", "192.168.88.1")
MT_PORT = int(env("MT_PORT", "8728"))
MT_USER = env("MT_USER", "monitor")
MT_PASS = env("MT_PASS", "")
MT_SSL = env("MT_SSL", "0") == "1"
MT_SSL_VERIFY = env("MT_SSL_VERIFY", "0") == "1"

WANS = [
    {"id": "wan1", "iface": env("WAN1_IFACE", "ether1"), "label": env("WAN1_LABEL", "WAN 1")},
    {"id": "wan2", "iface": env("WAN2_IFACE", "ether2"), "label": env("WAN2_LABEL", "WAN 2")},
]

POLL_INTERVAL = float(env("POLL_INTERVAL", "2"))
HISTORY_SECONDS = int(env("HISTORY_SECONDS", "3600"))
LISTEN_HOST = env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(env("LISTEN_PORT", "8080"))
DASH_USER = env("DASH_USER", "")
DASH_PASS = env("DASH_PASS", "")
DEMO = env("DEMO", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wan-monitor")

# ---------------------------------------------------------------- estado
MAX_POINTS = int(HISTORY_SECONDS / POLL_INTERVAL) + 5
lock = threading.Lock()
status = {"connected": False, "error": None, "updated": None, "demo": DEMO}
state = {
    w["id"]: {"points": deque(maxlen=MAX_POINTS), "running": None, "found": False,
              "rx_counter": None, "tx_counter": None}
    for w in WANS
}


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "yes", "1")


# ---------------------------------------------------------------- fontes de dados
class MikroTikSource:
    def __init__(self):
        self.api = None

    def _connect(self):
        from librouteros import connect
        kwargs = dict(host=MT_HOST, username=MT_USER, password=MT_PASS, port=MT_PORT, timeout=10)
        if MT_SSL:
            ctx = ssl.create_default_context()
            if not MT_SSL_VERIFY:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl_wrapper"] = ctx.wrap_socket
        self.api = connect(**kwargs)
        log.info("Ligado ao MikroTik %s:%s como %s", MT_HOST, MT_PORT, MT_USER)

    def close(self):
        try:
            if self.api:
                self.api.close()
        except Exception:
            pass
        self.api = None

    def fetch(self):
        if self.api is None:
            self._connect()
        try:
            rows = list(self.api.path("interface"))
        except Exception:
            self.close()
            raise
        out = {}
        for r in rows:
            name = r.get("name")
            if name:
                out[name] = (int(r.get("rx-byte", 0) or 0), int(r.get("tx-byte", 0) or 0),
                             as_bool(r.get("running", False)))
        return out


class DemoSource:
    """Gera tráfego fictício para testar a página sem MikroTik."""
    def __init__(self):
        self.c = {w["iface"]: [random.randint(10**9, 10**10), random.randint(10**8, 10**9)] for w in WANS}
        self.t0 = time.time()

    def fetch(self):
        t = time.time() - self.t0
        out = {}
        for i, w in enumerate(WANS):
            base_dl = (60 if i == 0 else 25) * 1e6
            dl = base_dl * (0.6 + 0.4 * math.sin(t / (20 + i * 7))) * random.uniform(0.7, 1.3)
            ul = dl * random.uniform(0.08, 0.2)
            c = self.c[w["iface"]]
            c[0] += int(dl / 8 * POLL_INTERVAL)
            c[1] += int(ul / 8 * POLL_INTERVAL)
            out[w["iface"]] = (c[0], c[1], True)
        return out


# ---------------------------------------------------------------- recolha
def poller():
    source = DemoSource() if DEMO else MikroTikSource()
    prev = {}
    backoff = POLL_INTERVAL
    while True:
        t = time.time()
        try:
            data = source.fetch()
            with lock:
                status.update(connected=True, error=None, updated=int(t * 1000))
                for w in WANS:
                    s = state[w["id"]]
                    d = data.get(w["iface"])
                    if d is None:
                        s["found"] = False
                        continue
                    rx, tx, running = d
                    s.update(found=True, running=running, rx_counter=rx, tx_counter=tx)
                    p = prev.get(w["id"])
                    if p:
                        dt = t - p[0]
                        drx, dtx = rx - p[1], tx - p[2]
                        # contador reiniciado (reboot) -> ignora esta amostra
                        if dt > 0 and drx >= 0 and dtx >= 0:
                            s["points"].append([int(t * 1000), round(drx * 8 / dt), round(dtx * 8 / dt)])
                    prev[w["id"]] = (t, rx, tx)
            backoff = POLL_INTERVAL
        except Exception as e:
            log.warning("Erro a ler o MikroTik: %s", e)
            with lock:
                status.update(connected=False, error=str(e))
            prev.clear()
            backoff = min(backoff * 2, 30)
            time.sleep(backoff)
            continue
        time.sleep(max(0.0, POLL_INTERVAL - (time.time() - t)))


# ---------------------------------------------------------------- web
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")


@app.before_request
def basic_auth():
    if not DASH_USER:
        return None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
            if u == DASH_USER and p == DASH_PASS:
                return None
        except Exception:
            pass
    return Response("Autenticação necessária", 401, {"WWW-Authenticate": 'Basic realm="WAN Monitor"'})


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/data")
def api_data():
    since = request.args.get("since", type=int, default=0)
    with lock:
        wans = []
        for w in WANS:
            s = state[w["id"]]
            wans.append({
                **w,
                "found": s["found"],
                "running": s["running"],
                "rx_counter": s["rx_counter"],
                "tx_counter": s["tx_counter"],
                "points": [p for p in s["points"] if p[0] > since],
            })
        return jsonify({"status": dict(status), "interval": POLL_INTERVAL,
                        "history_seconds": HISTORY_SECONDS, "router": MT_HOST, "wans": wans})


@app.route("/healthz")
def healthz():
    with lock:
        return jsonify(ok=status["connected"], error=status["error"])


def main():
    threading.Thread(target=poller, daemon=True).start()
    from waitress import serve
    log.info("A servir em http://%s:%s %s", LISTEN_HOST, LISTEN_PORT, "(modo DEMO)" if DEMO else "")
    serve(app, host=LISTEN_HOST, port=LISTEN_PORT, threads=4)


if __name__ == "__main__":
    main()
