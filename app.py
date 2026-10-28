#!/usr/bin/env python3
"""
WAN Monitor - mostra o tráfego das WAN de um (ou mais) MikroTik em tempo real.
Lê os contadores rx-byte / tx-byte das interfaces via API RouterOS (porta 8728/8729)
usando um utilizador apenas de leitura, e calcula as taxas no servidor.

Cada WAN é definida por WANn_IFACE (n = 1..9). Por defeito todas são lidas do router
MT_HOST; uma WAN pode vir de outro router com WANn_HOST / WANn_USER / WANn_PASS ...
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
    v = os.environ.get(key)
    return default if v is None or v == "" else v

MT_HOST = env("MT_HOST", "192.168.88.1")
MT_PORT = int(env("MT_PORT", "8728"))
MT_USER = env("MT_USER", "monitor")
MT_PASS = os.environ.get("MT_PASS", "")
MT_SSL = env("MT_SSL", "0") == "1"
MT_SSL_VERIFY = env("MT_SSL_VERIFY", "0") == "1"

POLL_INTERVAL = float(env("POLL_INTERVAL", "2"))
HISTORY_SECONDS = int(env("HISTORY_SECONDS", "3600"))
LISTEN_HOST = env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(env("LISTEN_PORT", "8080"))
DASH_USER = env("DASH_USER", "")
DASH_PASS = env("DASH_PASS", "")
DEMO = env("DEMO", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wan-monitor")


def load_wans():
    """WAN1 e WAN2 existem sempre (compatibilidade); WAN3..WAN9 só se WANn_IFACE estiver definido."""
    wans = []
    for n in range(1, 10):
        iface = env(f"WAN{n}_IFACE", {1: "ether1", 2: "ether2"}.get(n))
        if not iface:
            continue
        p = f"WAN{n}_"
        wans.append({
            "id": f"wan{n}",
            "iface": iface,
            "label": env(p + "LABEL", f"WAN {n}"),
            "gateway": env(p + "GATEWAY", ""),
            "conn": {
                "host": env(p + "HOST", MT_HOST),
                "port": int(env(p + "PORT", str(MT_PORT))),
                "user": env(p + "USER", MT_USER),
                "password": os.environ.get(p + "PASS", MT_PASS),
                "ssl": env(p + "SSL", "1" if MT_SSL else "0") == "1",
                "verify": env(p + "SSL_VERIFY", "1" if MT_SSL_VERIFY else "0") == "1",
            },
        })
    return wans


WANS = load_wans()

# ---------------------------------------------------------------- estado
MAX_POINTS = int(HISTORY_SECONDS / POLL_INTERVAL) + 5
lock = threading.Lock()
status = {"connected": False, "error": None, "updated": None, "demo": DEMO}
state = {
    w["id"]: {"points": deque(maxlen=MAX_POINTS), "running": None, "found": False,
              "rx_counter": None, "tx_counter": None, "online": False, "error": None}
    for w in WANS
}


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "yes", "1")


# ---------------------------------------------------------------- fontes de dados
class MikroTikSource:
    def __init__(self, conn):
        self.c = conn
        self.api = None

    @property
    def name(self):
        return f"{self.c['host']}:{self.c['port']}"

    def _connect(self):
        from librouteros import connect
        kwargs = dict(host=self.c["host"], username=self.c["user"], password=self.c["password"],
                      port=self.c["port"], timeout=10)
        if self.c["ssl"]:
            ctx = ssl.create_default_context()
            if not self.c["verify"]:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl_wrapper"] = ctx.wrap_socket
        self.api = connect(**kwargs)
        log.info("Ligado ao MikroTik %s como %s", self.name, self.c["user"])

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
    def __init__(self, wans):
        self.wans = wans
        self.c = {w["iface"]: [random.randint(10**9, 10**10), random.randint(10**8, 10**9)] for w in wans}
        self.t0 = time.time()
        self.name = "demo"

    def fetch(self):
        t = time.time() - self.t0
        out = {}
        for w in self.wans:
            i = int(w["id"][3:]) - 1
            base_dl = [60, 25, 40, 15, 10, 10, 10, 10, 10][i] * 1e6
            dl = base_dl * (0.6 + 0.4 * math.sin(t / (20 + i * 7))) * random.uniform(0.7, 1.3)
            ul = dl * random.uniform(0.08, 0.2)
            c = self.c[w["iface"]]
            c[0] += int(dl / 8 * POLL_INTERVAL)
            c[1] += int(ul / 8 * POLL_INTERVAL)
            out[w["iface"]] = (c[0], c[1], True)
        return out


def build_groups():
    """Agrupa as WAN por router, para fazer uma só leitura por router em cada ciclo."""
    groups = {}
    for w in WANS:
        c = w["conn"]
        key = "demo-" + w["id"] if DEMO else (c["host"], c["port"], c["user"], c["password"], c["ssl"], c["verify"])
        if key not in groups:
            src = DemoSource([]) if DEMO else MikroTikSource(c)
            groups[key] = {"source": src, "wans": []}
        groups[key]["wans"].append(w)
        if DEMO:
            groups[key]["source"] = DemoSource(groups[key]["wans"])
    return list(groups.values())


# ---------------------------------------------------------------- recolha
def poll_group(g):
    source, wans = g["source"], g["wans"]
    prev = {}
    backoff = POLL_INTERVAL
    while True:
        t = time.time()
        try:
            data = source.fetch()
            with lock:
                for w in wans:
                    s = state[w["id"]]
                    s.update(online=True, error=None)
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
                refresh_status(int(t * 1000))
            backoff = POLL_INTERVAL
        except Exception as e:
            log.warning("Erro a ler o MikroTik %s: %s", source.name, e)
            with lock:
                for w in wans:
                    state[w["id"]].update(online=False, error=f"{source.name}: {e}")
                refresh_status(None)
            prev.clear()
            backoff = min(backoff * 2, 30)
            time.sleep(backoff)
            continue
        time.sleep(max(0.0, POLL_INTERVAL - (time.time() - t)))


def refresh_status(updated):
    """Chamar com o lock. Ligado = todos os routers respondem."""
    errs = sorted({s["error"] for s in state.values() if s["error"]})
    status.update(connected=not errs, error="; ".join(errs) or None)
    if updated:
        status["updated"] = updated


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
                "id": w["id"], "iface": w["iface"], "label": w["label"],
                "gateway": w["gateway"], "router": w["conn"]["host"],
                "online": s["online"], "error": s["error"],
                "found": s["found"], "running": s["running"],
                "rx_counter": s["rx_counter"], "tx_counter": s["tx_counter"],
                "points": [p for p in s["points"] if p[0] > since],
            })
        routers = sorted({w["conn"]["host"] for w in WANS})
        return jsonify({"status": dict(status), "interval": POLL_INTERVAL,
                        "history_seconds": HISTORY_SECONDS, "router": ", ".join(routers), "wans": wans})


@app.route("/healthz")
def healthz():
    with lock:
        return jsonify(ok=status["connected"], error=status["error"])


def main():
    for g in build_groups():
        threading.Thread(target=poll_group, args=(g,), daemon=True).start()
    log.info("A monitorizar %d WAN: %s", len(WANS),
             ", ".join(f"{w['label']} ({w['conn']['host']}/{w['iface']})" for w in WANS))
    from waitress import serve
    log.info("A servir em http://%s:%s %s", LISTEN_HOST, LISTEN_PORT, "(modo DEMO)" if DEMO else "")
    serve(app, host=LISTEN_HOST, port=LISTEN_PORT, threads=4)


if __name__ == "__main__":
    main()
