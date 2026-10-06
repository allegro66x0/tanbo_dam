"""操作画面の目視確認用デモ。偽 XBee・偽 GAS・過去24時間分の合成データで親機と画面を起動する。

  python tests/demo_web.py [port]      → http://127.0.0.1:<port>/
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_parent import FakeGas, make_cfg  # noqa: E402
from xbee_emu import ChildModel, XBeeEmulator  # noqa: E402

from tanbo.main import App  # noqa: E402
from tanbo.store import Store  # noqa: E402
from tanbo.web import serve  # noqa: E402

MACS = {i: f"0013A200423E{0xA000 + i:04X}" for i in range(13)}
KINDS = {2: "dead", 7: "flaky", 9: "noecho", 11: "v1"}


def seed(db_path: str, base: dict[int, float]) -> None:
    st = Store(db_path)
    now = time.time()
    t = (now // 600) * 600 - 24 * 3600
    rows, health = [], []
    rnd = random.Random(1)
    while t < now - 60:
        n_ok = 0
        for n in range(13):
            ts = t + 0.3 + n * 0.2
            r = {"cycle_ts": t, "ts": ts, "synced": 1, "boot_id": "demo", "node": n,
                 "mac": MACS[n], "seq": int(t) & 0xFFFF}
            # 夕方に雨 → 水位上昇(距離が縮む)
            rain = 14 * math.exp(-((now - t) / 3600 - 7) ** 2 / 4)
            d = base[n] - rain + rnd.gauss(0, 0.4)
            kind = KINDS.get(n, "v2")
            fail = (kind == "dead" and (now - t) < 5 * 3600) or (kind == "flaky" and rnd.random() < 0.25) \
                or (12 * 3600 < now - t < 13.5 * 3600)   # 親機の電源断
            if 12 * 3600 < now - t < 13.5 * 3600:
                continue
            if fail:
                r.update(status="TIMEOUT" if kind == "flaky" else "TX_FAIL",
                         tx_status=0 if kind == "flaky" else 0x21)
            elif kind == "noecho" and (now - t) < 3 * 3600:
                r.update(status="NO_ECHO", n_ok=0, n_try=7, child_uptime_s=50000, fw="2.0.0")
            elif kind == "v1":
                r.update(status="OK_V1", dist_cm=round(d, 1), fw="v1")
                n_ok += 1
            else:
                us = int(d * 2 / 0.03434)
                spread = 6 if n != 5 else rnd.choice([6, 6, 6, 400])
                r.update(status="OK", n_ok=7, n_try=7, med_us=us, min_us=us - spread,
                         max_us=us + spread, dist_cm=round(us * 0.03434 / 2, 2),
                         child_uptime_s=int(86400 - (now - t)), fw="2.0.0", tx_status=0,
                         rtt_ms=rnd.randint(140, 420))
                n_ok += 1
            r["sent"] = 1
            rows.append(r)
        health.append({"ts": t + 4, "synced": 1, "boot_id": "demo", "uptime_s": 86400 - (now - t),
                       "interval_s": 600, "cycle_ms": 4200, "n_ok": n_ok, "n_nodes": 13,
                       "cpu_temp": 48.0 + rnd.random() * 4, "load1": 0.2, "mem_avail_mb": 3100,
                       "disk_free_mb": 21000, "queue_m": 0, "queue_h": 0, "batt_v": 12.6,
                       "xbee_ai": 0, "fw": "2.0.0"})
        t += 600
    st.add_measurements(rows)
    for h in health:
        st.add_health(h)
    with st._lock:
        st._db.execute("UPDATE measurements SET sent=1")
        st._db.execute("UPDATE health SET sent=1")
    st.close()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    tmp = tempfile.mkdtemp()
    base = {n: 45 + 3 * n for n in range(13)}
    children = {MACS[n]: ChildModel(KINDS.get(n, "v2"), echo_us=int(base[n] * 2 / 0.03434))
                for n in range(13)}
    children[MACS[9]].kind = "noecho"
    emu = XBeeEmulator(children)
    gas = FakeGas()
    cfg = make_cfg(tmp, emu.port, gas.url, MACS)
    seed(cfg.storage.db_path, base)
    app = App(cfg)
    threading.Thread(target=app.run, daemon=True).start()
    cfg.web.allow_interfaces = []
    srv = serve(cfg, "127.0.0.1", port)
    print(f"http://127.0.0.1:{port}/  tmp={tmp}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
