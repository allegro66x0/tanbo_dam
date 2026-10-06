"""親機の結合テスト: 実 digi-xbee + pty エミュレータ + 偽 GAS(HTTP)。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from xbee_emu import COORD_MAC, ChildModel, XBeeEmulator  # noqa: E402

from tanbo import config as config_mod  # noqa: E402
from tanbo import protocol  # noqa: E402
from tanbo.control import call, parse_duration  # noqa: E402
from tanbo.main import App  # noqa: E402
from tanbo.poller import PollResult, Schedule, summarize_failures  # noqa: E402
from tanbo.uploader import UploadRejected, describe_error  # noqa: E402

MACS = {
    0: "0013A200423ECC65", 1: "0013A200423ECBC6", 2: "0013A200423EC0CD",
    3: "0013A200423EB483", 4: "0013A200423E9827", 5: "0013A200423EB80C",
    6: "0013A200423EB0A3", 7: "0013A200423EB488",
}


# ---------------------------------------------------------------- fake GAS
class FakeGas:
    """GAS(Code.gs)と同じ高水位線の意味論を持つ HTTP サーバ。"""

    def __init__(self, token="t0k"):
        self.token = token
        self.rows = {"m": [], "h": []}
        self.hwm: dict[tuple[str, str], int] = {}
        self.fail_next = 0
        self.requests = []
        # direct: POST にそのまま JSON を返す
        # echo:   本物の GAS と同じく POST /exec → 302 → GET /macros/echo で結果
        # hop:    その前に /exec → /exec2 の転送が1回挟まる(requests 任せだと GET /exec2 = doGet になる)
        self.mode = "direct"
        self._echo: dict[str, bytes] = {}
        gas = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send_json(self, data: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, loc: str):
                self.send_response(302)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path.startswith("/macros/echo?k="):
                    data = gas._echo.pop(self.path.split("=", 1)[1], None)
                    if data is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._send_json(data)
                else:   # doGet
                    self._send_json(json.dumps({"ok": True, "version": "fake"}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if gas.mode == "hop" and self.path == "/exec":
                    self._redirect("/exec2")
                    return
                gas.requests.append(body)
                if gas.fail_next:
                    gas.fail_next -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                if body.get("token") != gas.token:
                    out = {"ok": False, "error": "bad token"}
                else:
                    key = (body["db_uuid"], body["kind"])
                    h = gas.hwm.get(key, 0)
                    new = [r for r in body["rows"] if r["id"] > h]
                    gas.rows[body["kind"]].extend(new)
                    if new:
                        h = max(r["id"] for r in new)
                    gas.hwm[key] = h
                    out = {"ok": True, "hwm": h, "added": len(new)}
                data = json.dumps(out).encode()
                if gas.mode in ("echo", "hop"):
                    k = str(len(gas.requests))
                    gas._echo[k] = data
                    self._redirect(f"/macros/echo?k={k}")
                    return
                self._send_json(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/exec"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


# ---------------------------------------------------------------- helpers
def make_cfg(tmp, port, url, nodes, **sched):
    sock = os.path.join(tmp, "ctl.sock")
    toml = f"""
[xbee]
port = "{port}"
reply_timeout_s = 1.0
gap_s = 0.02
[schedule]
interval_s = {sched.get('interval_s', 600)}
check_interval_s = {sched.get('check_interval_s', 60)}
[upload]
url = "{url}"
token = "t0k"
period_s = 1
[storage]
db_path = "{tmp}/tanbo.db"
[control]
socket = "{sock}"
[nodes]
""" + "\n".join(f'{n} = "{m}"' for n, m in nodes.items())
    p = os.path.join(tmp, "parent.toml")
    with open(p, "w") as f:
        f.write(toml)
    return config_mod.load(p)


@pytest.fixture
def env():
    tmp = tempfile.mkdtemp()
    gas = FakeGas()
    children = {
        MACS[0]: ChildModel("v2", echo_us=3500),
        MACS[1]: ChildModel("v1", echo_us=4000),
        MACS[2]: ChildModel("dead"),
        MACS[3]: ChildModel("mute"),
        MACS[4]: ChildModel("garbage"),
        MACS[5]: ChildModel("noecho"),
        MACS[6]: ChildModel("late", late_delay=1.4),
    }
    emu = XBeeEmulator(children)
    cfg = make_cfg(tmp, emu.port, gas.url, {n: MACS[n] for n in range(7)})
    yield tmp, gas, emu, cfg, children
    emu.close()
    gas.close()


# ---------------------------------------------------------------- unit tests
def test_protocol_parse():
    r = protocol.parse_reply(b"D2,12,6,7,3500,3480,3530,99,2.0.0\n")
    assert (r.version, r.seq, r.n_ok, r.med_us) == (2, 12, 6, 3500)
    assert abs(r.dist_cm - 3500 * 0.03434 / 2) < 0.01
    r = protocol.parse_reply(b"D2,-1,0,7,-1,-1,-1,5,2.0.0")
    assert r.seq is None and r.n_ok == 0 and r.med_us is None and r.dist_cm is None
    assert protocol.parse_reply(b"ID3,12.3,-999.0,-999.0,-999.0\n").dist_cm == 12.3
    assert protocol.parse_reply(b"ID,-999.0,-999.0,-999.0,-999.0").dist_cm is None
    assert protocol.parse_reply(b"ID,NoData").dist_cm is None
    for bad in (b"", b"\xff\x00", b"D2,1,2", b"D2,1,8,7,1,1,1,1,x", b"hello"):
        with pytest.raises(protocol.ParseError):
            protocol.parse_reply(bad)
    assert protocol.build_req(70000) == b"REQ,4464\n"


def test_schedule_alignment():
    s = Schedule(600)
    assert s.next_slot(1000.0) == 1200.0
    assert s.next_slot(1200.0) == 1800.0
    until = s.set_override(60, 300)
    now = time.time()
    nxt = s.next_slot(now)
    assert nxt % 60 == 0 and 0 < nxt - now <= 60
    # 期限後は通常周期に戻る
    assert s.current(until + 1) == (600, None)
    assert s.next_slot(until + 1) % 600 == 0
    # 期限までに短周期の枠がない場合は通常周期の境界
    s.set_override(60, 5)
    t = (time.time() // 60) * 60 + 58   # 枠の 2 秒前、期限は約 5 秒後なのでこの枠は期限内
    assert s.next_slot(t) % 60 == 0


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200


def test_summarize_failures():
    def r(node, status, tx=None):
        return PollResult(node, "X", status, {"tx_status": tx})
    res = [r(1, "TX_FAIL", 0x24), r(2, "OK_V1", 0), r(3, "TX_FAIL", 0x24), r(4, "TX_FAIL", 0x24),
           r(5, "TIMEOUT", 0), r(6, "OK", 0), r(7, "TX_FAIL", 0x21), r(12, "TX_FAIL", 0x24)]
    assert summarize_failures(res) == "TX_FAIL(0x24): 1,3-4,12; TIMEOUT: 5; TX_FAIL(0x21): 7"
    assert summarize_failures([r(1, "OK"), r(2, "OK_V1")]) == ""


def test_describe_error():
    import requests
    def err(url, **kw):
        try:
            requests.post(url, json={}, timeout=3, **kw)
        except Exception as e:
            return describe_error(e)
    # 接続拒否: 文面の末尾にある原因が残る(以前は 200 文字切りで落ちていた)
    d = err("http://127.0.0.1:9/x")
    assert d.startswith("NET: ") and "Connection refused" in d and len(d) < 120, d
    assert describe_error(UploadRejected("GAS", "bad token")) == "GAS: bad token"
    assert describe_error(requests.exceptions.ReadTimeout("x")) == "TIMEOUT: read"
    assert describe_error(requests.exceptions.ConnectTimeout("x")) == "TIMEOUT: connect"
    resp = requests.Response(); resp.status_code = 404; resp.reason = "Not Found"
    assert describe_error(requests.exceptions.HTTPError(response=resp)) == "HTTP: 404 Not Found"
    assert describe_error(ValueError("boom")) == "ERR: ValueError: boom"


# ---------------------------------------------------------------- integration
def test_cycle_statuses_and_upload(env):
    tmp, gas, emu, cfg, children = env
    app = App(cfg)
    app.radio.open()
    t0 = time.monotonic()
    res = app.poller.run_cycle(time.time())
    dt = time.monotonic() - t0
    st = {r.node: r.status for r in res}
    assert st == {0: "OK", 1: "OK_V1", 2: "TX_FAIL", 3: "TIMEOUT", 4: "BAD_REPLY",
                  5: "NO_ECHO", 6: "TIMEOUT"}, st
    rows = {r.node: r.row for r in res}
    assert rows[0]["med_us"] == 3500 and abs(rows[0]["dist_cm"] - 60.1) < 0.1
    assert rows[0]["seq"] is not None and rows[0]["tx_status"] == 0
    assert rows[2]["tx_status"] == 0x21
    assert rows[4]["raw"]
    # 死んだノードは Transmit Status で即打ち切り(タイムアウトを待たない)
    assert dt < 1.0 * 4 + 1.5, dt
    # 子機は seq 付き REQ を受け取っている
    assert children[MACS[0]].requests[-1].startswith(b"REQ,")

    # 2サイクル目: ノード6の遅延応答(前回 seq)は捨て、今回も TIMEOUT
    time.sleep(0.6)
    res2 = app.poller.run_cycle(time.time())
    assert {r.node: r.status for r in res2}[6] == "TIMEOUT"

    # アップロード: 1回目は 500 で失敗 → 未送信のまま、2回目で送信済み
    gas.fail_next = 1
    with pytest.raises(Exception) as ei:
        app.uploader.upload_once()
    assert describe_error(ei.value).startswith("HTTP: 500"), describe_error(ei.value)
    assert app.store.unsent_count("m") == 14
    assert app.uploader.last_ok is None      # 「最終送信成功」は受理された POST だけで進む
    n = app.uploader.upload_once()
    assert app.uploader.last_ok is not None
    assert n == 14 + 2
    assert app.store.unsent_count("m") == 0 and app.store.unsent_count("h") == 0
    assert len(gas.rows["m"]) == 14 and len(gas.rows["h"]) == 2
    # 再送しても GAS 側で重複しない
    rows_m = app.store.fetch_unsent("m", 10)
    assert rows_m == []
    with app.store._lock:
        app.store._db.execute("UPDATE measurements SET sent=0")
    app.uploader.upload_once()
    assert len(gas.rows["m"]) == 14
    h = gas.rows["h"][0]
    assert h["n_nodes"] == 7 and h["n_ok"] == 2 and h["xbee_ai"] == 0
    json.dump(gas.requests[-3:], open(os.path.join(tmp, "payloads.json"), "w"))
    app.radio.close()


def test_own_address_is_skipped(env):
    # [nodes] に親機 XBee 自身のアドレスがあっても、折り返しを BAD_REPLY として記録しない
    tmp, gas, emu, cfg, children = env
    cfg.nodes[7] = COORD_MAC.hex().upper()
    app = App(cfg)
    app.radio.open()
    res = app.poller.run_cycle(time.time())
    assert sorted(r.node for r in res) == list(range(7))
    assert app.poller.last_cycle["n_nodes"] == 7
    app.radio.close()


def test_bad_token(env):
    tmp, gas, emu, cfg, _ = env
    cfg.upload.token = "wrong"
    app = App(cfg)
    app.poller.run_cycle(time.time())
    with pytest.raises(RuntimeError, match="bad token"):
        app.uploader.upload_once()
    assert app.store.unsent_count("m") == 7
    app.radio.close()


def test_radio_loss_and_recovery(env):
    tmp, gas, emu, cfg, _ = env
    app = App(cfg)
    assert app.poller.run_cycle(time.time())[0].status == "OK"
    emu.dead = True       # XBee が応答しなくなる
    res = app.poller.run_cycle(time.time())
    assert all(r.status == "RADIO_ERR" for r in res)
    assert not app.radio.is_open
    emu.dead = False
    app.poller._radio_retry_at = 0      # 再オープン待ちを短縮
    res = app.poller.run_cycle(time.time())
    assert res[0].status == "OK"
    app.radio.close()


@pytest.mark.parametrize("mode", ["echo", "hop"])
def test_upload_follows_gas_redirects(env, mode):
    tmp, gas, emu, cfg, _ = env
    gas.mode = mode
    app = App(cfg)
    app.poller.run_cycle(time.time())
    assert app.uploader.upload_once() == 7 + 1
    assert len(gas.rows["m"]) == 7 and len(gas.rows["h"]) == 1
    app.radio.close()


def test_missing_hwm_is_classified(env):
    # doGet の応答が返ってきた場合(KeyError ではなく RESP に分類)
    tmp, gas, emu, cfg, _ = env
    cfg.upload.url = cfg.upload.url.replace("/exec", "/exec?x")
    app = App(cfg)
    app.poller.run_cycle(time.time())
    orig = app.uploader._request_gas

    def as_get(body):
        r, _ = orig(body)
        return app.uploader._session.get(cfg.upload.url, timeout=5), "test"
    app.uploader._request_gas = as_get
    with pytest.raises(UploadRejected) as ei:
        app.uploader.upload_once()
    assert describe_error(ei.value).startswith("RESP: 応答に hwm がない")
    app.radio.close()


def test_unplugged_xbee_does_not_restart(env, monkeypatch):
    tmp, gas, emu, cfg, _ = env
    import tanbo.poller as poller_mod
    monkeypatch.setattr(poller_mod, "RADIO_FATAL_AFTER_S", 0)
    app = App(cfg)
    emu.dead = True
    # デバイスはあるが応答しない → 再起動を要求
    app.poller.run_cycle(time.time())
    app.poller._radio_retry_at = 0
    app.poller.run_cycle(time.time())
    assert app.poller.fatal
    # 抜けている(ポートのパスがない)→ 再起動しても直らないので要求しない
    app.poller.fatal = None
    cfg.xbee.port = os.path.join(tmp, "no-such-port")
    app.radio.port = cfg.xbee.port
    app.poller._radio_retry_at = 0
    res = app.poller.run_cycle(time.time())
    assert all(r.status == "RADIO_ERR" for r in res)
    assert app.poller.fatal is None


def test_service_and_ctl(env):
    tmp, gas, emu, cfg, _ = env
    app = App(cfg)
    th = threading.Thread(target=app.run, daemon=True)
    th.start()
    sock = cfg.control.socket
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.1)

    s = call(sock, "status")
    assert s["interval_s"] == 600 and s["next_slot"] % 600 == 0

    r = call(sock, "check", {"duration": 120})
    assert r["interval_s"] == 60
    time.sleep(0.8)
    s = call(sock, "status")
    assert s["interval_s"] == 60 and s["next_slot"] % 60 == 0 and s["override_until"]

    assert "error" in call(sock, "interval", {"seconds": 5, "duration": 30})
    r = call(sock, "interval", {"seconds": 10, "duration": 60})
    assert r["interval_s"] == 10
    assert "error" in call(sock, "interval", {"seconds": 10, "duration": 10 ** 6})

    g = call(sock, "get", {"node": 0})
    assert g["status"] == "OK" and g["med_us"] == 3500
    assert "error" in call(sock, "get", {"node": 99})

    p = call(sock, "poll")
    assert p["0"] == "OK" and p["2"] == "TX_FAIL"

    # 10秒周期で自動サイクルが回り、アップロードされる
    deadline = time.time() + 25
    while time.time() < deadline and app.store.unsent_count("h") + len(gas.rows["h"]) < 2:
        time.sleep(0.5)
    s = call(sock, "status")
    assert s["last_cycle"]["n_nodes"] == 7
    assert [x["status"] for x in s["latest"]][:2] == ["OK", "OK_V1"]
    app.uploader.wake.set()
    deadline = time.time() + 30
    while time.time() < deadline and len(gas.rows["m"]) < 14:
        time.sleep(0.3)
    assert len(gas.rows["m"]) >= 14

    r = call(sock, "normal")
    assert r["interval_s"] == 600
    assert call(sock, "status")["interval_s"] == 600

    app.stopping = True
    th.join(timeout=60)
    assert not th.is_alive()
    assert not os.path.exists(sock)
