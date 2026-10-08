"""操作画面(tanbo.web)のテスト。親機サービスは実物(偽 XBee 相手)を動かす。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_parent import MACS, FakeGas, make_cfg  # noqa: E402
from xbee_emu import ChildModel, XBeeEmulator  # noqa: E402

from tanbo.main import App  # noqa: E402
from tanbo.web import InterfaceAllowlist, serve  # noqa: E402


def req(base, path, body=None, header=True):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    if data:
        r.add_header("Content-Type", "application/json")
        if header:
            r.add_header("X-Tanbo-Request", "1")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


@pytest.fixture
def stack():
    tmp = tempfile.mkdtemp()
    gas = FakeGas()
    emu = XBeeEmulator({MACS[0]: ChildModel("v2"), MACS[1]: ChildModel("dead"),
                        MACS[2]: ChildModel("v1")})
    cfg = make_cfg(tmp, emu.port, gas.url, {0: MACS[0], 1: MACS[1], 2: MACS[2]})
    cfg.web.allow_interfaces = ["lo"]
    app = App(cfg)
    th = threading.Thread(target=app.run, daemon=True)
    th.start()
    for _ in range(50):
        if os.path.exists(cfg.control.socket):
            break
        time.sleep(0.1)
    srv = serve(cfg, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield cfg, app, th, srv, base
    srv.shutdown()
    if th.is_alive():
        app.stopping = True
        th.join(timeout=60)
    emu.close()
    gas.close()


def test_page_and_overview(stack):
    cfg, app, th, srv, base = stack
    code, html = req(base, "/")
    assert code == 200 and b"<title>" in html and "田んぼダム".encode() in html

    code, ov = req(base, "/api/overview")
    assert code == 200 and ov["service"]["interval_s"] == 600 and ov["service_error"] is None
    assert [n["node"] for n in ov["nodes"]] == [0, 1, 2]
    assert all(n["last"] is None for n in ov["nodes"])   # まだ計測していない
    assert ov["check_interval_s"] == 60


def test_commands_and_history(stack):
    cfg, app, th, srv, base = stack
    # 独自ヘッダなしの POST は拒否(CSRF 対策)
    code, _ = req(base, "/api/cmd", {"cmd": "poll"}, header=False)
    assert code == 403
    code, r = req(base, "/api/cmd", {"cmd": "shutdown"})
    assert code == 400
    code, r = req(base, "/api/cmd", {"cmd": "interval", "args": {"seconds": "x"}})
    assert code == 400

    code, r = req(base, "/api/cmd", {"cmd": "get", "args": {"node": 0}})
    assert code == 200 and r["status"] == "OK" and r["med_us"] == 3500
    code, r = req(base, "/api/cmd", {"cmd": "get", "args": {"node": 99}})
    assert code == 400 and "設定にありません" in r["error"]

    code, r = req(base, "/api/cmd", {"cmd": "poll"})
    assert code == 200 and r == {"0": "OK", "1": "TX_FAIL", "2": "OK_V1"}
    code, r = req(base, "/api/cmd", {"cmd": "poll"})

    code, ov = req(base, "/api/overview")
    n0, n1, n2 = ov["nodes"]
    assert n0["last"]["status"] == "OK" and n0["fails"] == 0 and n0["last_ok_ts"]
    assert n1["last"]["status"] == "TX_FAIL" and n1["fails"] == 2 and n1["last_ok_ts"] is None
    assert n2["last"]["status"] == "OK_V1"
    assert ov["health"][0]["n_nodes"] == 3

    code, h = req(base, "/api/history?node=0&hours=1")
    assert code == 200 and len(h["rows"]) == 2 and h["cols"][2] == "dist_cm"
    assert abs(h["rows"][0][2] - 60.1) < 0.1
    assert req(base, "/api/history?node=42")[0] == 404
    assert req(base, "/api/history?node=x")[0] == 400

    code, r = req(base, "/api/cmd", {"cmd": "check", "args": {"duration": 1800}})
    assert code == 200 and r["interval_s"] == 60
    code, ov = req(base, "/api/overview")
    assert ov["service"]["interval_s"] == 60 and ov["service"]["override_until"]
    code, r = req(base, "/api/cmd", {"cmd": "normal"})
    assert code == 200 and req(base, "/api/overview")[1]["service"]["override_until"] is None


def test_service_down_still_shows_records(stack):
    cfg, app, th, srv, base = stack
    req(base, "/api/cmd", {"cmd": "poll"})
    app.stopping = True
    th.join(timeout=60)
    code, ov = req(base, "/api/overview")
    assert code == 200 and ov["service"] is None and "動いていません" in ov["service_error"]
    assert ov["nodes"][0]["last"]["status"] == "OK"     # DB からは読める
    code, r = req(base, "/api/cmd", {"cmd": "poll"})
    assert code == 503


def test_interface_allowlist(stack):
    cfg, app, th, srv, base = stack
    srv.app.allow = InterfaceAllowlist(["nonexistent0"])
    assert req(base, "/")[0] == 403
    assert req(base, "/api/cmd", {"cmd": "poll"})[0] == 403
    assert InterfaceAllowlist(["lo"]).allows("127.0.0.1")
    assert InterfaceAllowlist(["lo"]).allows("::ffff:127.0.0.1")
    assert not InterfaceAllowlist(["lo"]).allows("100.64.1.2")
    assert InterfaceAllowlist([]).allows("203.0.113.5")


def test_overview_includes_local_sensor(monkeypatch):
    from tanbo import local as local_mod
    from xbee_emu import UsbChildEmulator
    monkeypatch.setattr(local_mod, "BOOT_WAIT_S", 0)
    tmp = tempfile.mkdtemp()
    gas = FakeGas()
    emu = XBeeEmulator({MACS[0]: ChildModel("v2")})
    dev = UsbChildEmulator("v2")
    cfg = make_cfg(tmp, emu.port, gas.url, {0: MACS[0]},
                   extra=f'[local]\nport = "{dev.port}"\nnode = 11\n')
    cfg.web.allow_interfaces = ["lo"]
    app = App(cfg)
    app.poller.run_cycle(time.time())
    srv = serve(cfg, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        code, ov = req(base, "/api/overview")
        assert code == 200 and ov["local_node"] == 11
        assert [n["node"] for n in ov["nodes"]] == [0, 11]
        assert {n["node"]: n["last"]["status"] for n in ov["nodes"]} == {0: "OK", 11: "OK"}
        code, h = req(base, "/api/history?node=11&hours=1")
        assert code == 200 and h["rows"][-1][1] == "OK"
    finally:
        srv.shutdown()
        app.radio.close()
        app.local.close()
        emu.close()
        dev.close()
        gas.close()
