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

from xbee_emu import COORD_MAC, ChildModel, UsbChildEmulator, XBeeEmulator  # noqa: E402

from tanbo import config as config_mod  # noqa: E402
from tanbo import local as local_mod  # noqa: E402
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
        self.assign = None   # dict にすると応答に分担表を入れる(GAS の「分担」シート相当)
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
                    if gas.assign is not None:
                        out["assign"] = gas.assign
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
def make_cfg(tmp, port, url, nodes, extra="", **sched):
    sock = os.path.join(tmp, "ctl.sock")
    toml = f"""
[xbee]
port = "{port}"
reply_timeout_s = 1.0
gap_s = 0.02
retries = {sched.get('retries', 0)}
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
{extra}
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


def test_retry_within_cycle(env):
    tmp, gas, emu, cfg, children = env
    cfg = make_cfg(tmp, emu.port, gas.url, {n: MACS[n] for n in range(7)}, retries=1)
    children[MACS[3]].kind = "lossy"   # 1 回目の REQ への応答だけ落ちる
    app = App(cfg)
    app.radio.open()
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    # 応答が落ちたノードは、送り直しの REQ への応答で OK
    assert res[3].status == "OK" and len(children[MACS[3]].requests) == 2
    assert res[3].row["seq"] == int(children[MACS[3]].requests[1].split(b",")[1])
    # 前の REQ への応答が遅れて(送り直しの待ち時間の中で)届いたら、それを採用する
    assert res[6].status == "OK" and len(children[MACS[6]].requests) == 2
    assert res[6].row["seq"] == int(children[MACS[6]].requests[0].split(b",")[1])
    # 届かないノードは retries 回まで送り直して、最後の結果を残す。うまくいったノードは 1 回だけ
    assert res[2].status == "TX_FAIL" and len(children[MACS[2]].requests) == 0
    assert res[0].status == "OK" and len(children[MACS[0]].requests) == 1
    assert res[5].status == "NO_ECHO" and len(children[MACS[5]].requests) == 1   # 応答があれば送り直さない
    # 記録は 1 ノード 1 行のまま
    assert app.store.unsent_count("m") == 7
    app.radio.close()


def test_retries_config_validation(env):
    tmp, gas, emu, cfg, children = env
    assert cfg.xbee.retries == 0
    with pytest.raises(config_mod.ConfigError):
        make_cfg(tmp, emu.port, gas.url, {0: MACS[0]}, retries=9)


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


# ---------------------------------------------------------------- 親機の地点のセンサ([local])
LOCAL_NODE = 11


def local_cfg(env, usb_port):
    tmp, gas, emu, _, _ = env
    extra = f'[local]\nport = "{usb_port}"\nnode = {LOCAL_NODE}\nreply_timeout_s = 1.0\n'
    return make_cfg(tmp, emu.port, gas.url, {n: MACS[n] for n in range(7)}, extra=extra)


@pytest.fixture
def usb(monkeypatch):
    monkeypatch.setattr(local_mod, "BOOT_WAIT_S", 0)
    devs = []

    def make(kind="v2"):
        devs.append(UsbChildEmulator(kind))
        return devs[-1]
    yield make
    for d in devs:
        d.close()


def test_local_sensor_in_cycle(env, usb):
    tmp, gas, emu, _, _ = env
    dev = usb("v2")
    cfg = local_cfg(env, dev.port)
    assert cfg.node_ids() == [0, 1, 2, 3, 4, 5, 6, LOCAL_NODE]
    app = App(cfg)
    app.radio.open()
    res = app.poller.run_cycle(time.time())
    assert [r.node for r in res] == cfg.node_ids()
    r = {x.node: x for x in res}[LOCAL_NODE]
    assert r.status == "OK", r.row
    assert r.row["med_us"] == 2900 and r.row["mac"] == "LOCAL" and r.row["tx_status"] is None
    assert abs(r.row["dist_cm"] - 49.79) < 0.1 and r.row["fw"] == "2.1.0"
    assert dev.requests[-1] == b"REQ,%d" % r.row["seq"]
    assert {x.node: x.status for x in res}[0] == "OK"   # XBee 側はこれまでどおり
    # 記録されて GAS へも送られる
    app.uploader.upload_once()
    assert any(row["node"] == LOCAL_NODE and row["status"] == "OK" for row in gas.rows["m"])
    # 手動取得と status
    assert app.poller._handle_command("get", LOCAL_NODE)["status"] == "OK"
    s = app.handle_control("status", {})
    assert s["local_node"] == LOCAL_NODE and s["local_open"] and s["local_error"] is None
    app.radio.close()
    app.local.close()


@pytest.mark.parametrize("kind,status", [("noecho", "NO_ECHO"), ("mute", "TIMEOUT"),
                                         ("stale", "OK")])
def test_local_sensor_replies(env, usb, kind, status):
    dev = usb(kind)
    app = App(local_cfg(env, dev.port))
    r = app.poller.poll_local()
    assert r.status == status, r.row
    if kind == "stale":   # 前回 seq の応答は捨てて、今回の応答を採る
        assert r.row["med_us"] == 2900
    app.local.close()


def test_local_sensor_unplugged_and_back(env, usb):
    tmp = env[0]
    dev = usb("v2")
    cfg = local_cfg(env, os.path.join(tmp, "no-such-usb"))
    app = App(cfg)
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "PORT_ERR" and res[LOCAL_NODE].row["raw"]
    assert res[0].status == "OK"   # XBee 側の計測は止まらない
    assert app.poller.local_error and app.poller.fatal is None
    # 挿し直されたら次のサイクルで復帰
    cfg.local.port = app.local.port = dev.port
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "OK"
    assert app.poller.local_error is None
    app.radio.close()
    app.local.close()


def test_local_sensor_works_without_xbee(env, usb):
    tmp = env[0]
    dev = usb("v2")
    cfg = local_cfg(env, dev.port)
    cfg.xbee.port = os.path.join(tmp, "no-such-xbee")
    app = App(cfg)
    res = {r.node: r.status for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE] == "OK"
    assert all(s == "RADIO_ERR" for n, s in res.items() if n != LOCAL_NODE)
    app.local.close()


def test_local_node_must_not_overlap(env):
    tmp, gas, emu, _, _ = env
    with pytest.raises(config_mod.ConfigError, match="重なって"):
        make_cfg(tmp, emu.port, gas.url, {0: MACS[0]},
                 extra='[local]\nport = "/dev/null"\nnode = 0\n')


def test_local_sensor_io_error_recovers(env, usb):
    # 実機: USB が抜けて挿し直されたあと、開いたままの古いポートへの tcflush が termios.error(EIO)。
    # OSError の仲間ではないので取りこぼし、サイクルごと落ちていた
    import termios
    dev = usb("v2")
    app = App(local_cfg(env, dev.port))
    app.radio.open()
    assert app.poller.poll_local().status == "OK"

    def eio():
        raise termios.error(5, "Input/output error")
    app.local._ser.reset_input_buffer = eio
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "PORT_ERR"
    assert "Input/output error" in res[LOCAL_NODE].row["raw"]
    assert res[0].status == "OK"   # XBee の子機は記録される
    assert not app.local.is_open   # 閉じておき、次のサイクルで開き直す
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "OK" and app.poller.local_error is None
    app.radio.close()
    app.local.close()


def test_local_sensor_unexpected_error_keeps_xbee_rows(env, usb, monkeypatch):
    dev = usb("v2")
    app = App(local_cfg(env, dev.port))
    app.radio.open()
    monkeypatch.setattr(app.poller, "poll_local", lambda: 1 / 0)
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "PORT_ERR"
    assert "ZeroDivisionError" in res[LOCAL_NODE].row["raw"]
    assert res[0].status == "OK"
    assert app.store.unsent_count("m") == 8
    app.radio.close()


# ---------------------------------------------------------------- 親機の地点のセンサ(GPIO、par2)
class FakeGpio(local_mod.GpioSensor):
    """GPIO の代わりに、決めたエコー時間を順に返す。"""

    def __init__(self, samples, fail_open=False):
        super().__init__("/dev/null", 23, 24)
        self.samples = list(samples)
        self.fail_open = fail_open
        self.opened = 0

    def open(self):
        if self.fail_open:
            raise local_mod.LocalSensorError("GPIO を開けない (テスト)")
        self.opened += 1
        self._req = object()

    def close(self):
        self._req = None

    def ping_us(self):
        return self.samples.pop(0) if self.samples else 0


def gpio_cfg(env, extra=""):
    tmp, gas, emu, _, _ = env
    return make_cfg(tmp, emu.port, gas.url, {n: MACS[n] for n in range(7)},
                    extra=f'[local]\ntype = "gpio"\nnode = {LOCAL_NODE}\n{extra}')


def test_summarize_echo_matches_firmware():
    # 子機ファームと同じ: 範囲外(0、290 未満、11650 超)を捨て、偶数個なら中央 2 つの平均
    assert local_mod.summarize_echo([0, 0, 0]) == (0, None, None, None)
    assert local_mod.summarize_echo([3000, 0, 2900, 3100, 200, 20000, 3050]) == (4, 3025, 2900, 3100)
    assert local_mod.summarize_echo([1500, 1400, 1600]) == (3, 1500, 1400, 1600)


def test_local_gpio_config():
    tmp = tempfile.mkdtemp()
    base = f'[xbee]\nport = "/x"\n[upload]\nurl = "http://x"\ntoken = "t"\n[storage]\ndb_path = "{tmp}/d.db"\n'
    p = os.path.join(tmp, "c.toml")

    def load(local):
        with open(p, "w") as f:
            f.write(base + local)
        return config_mod.load(p)

    lc = load('[local]\ntype = "gpio"\nnode = 21\n').local
    assert (lc.type, lc.chip, lc.trig, lc.echo) == ("gpio", "/dev/gpiochip0", 23, 24)
    assert load('[local]\nport = "/dev/ttyUSB1"\nnode = 11\n').local.type == "usb"   # 書かなければ従来の USB
    with pytest.raises(config_mod.ConfigError, match="port"):
        load('[local]\nnode = 11\n')
    with pytest.raises(config_mod.ConfigError, match="trig"):
        load('[local]\ntype = "gpio"\nnode = 21\ntrig = 24\necho = 24\n')
    with pytest.raises(config_mod.ConfigError, match="usb / gpio"):
        load('[local]\ntype = "i2c"\nnode = 21\n')


def test_local_gpio_in_cycle(env, monkeypatch):
    monkeypatch.setattr(local_mod, "PING_GAP_S", 0)
    tmp, gas, emu, _, _ = env
    cfg = gpio_cfg(env)
    app = App(cfg)
    assert isinstance(app.local, local_mod.GpioSensor)
    fake = FakeGpio([2400, 2410, 0, 2390, 2405, 2395, 50000])
    app.local = app.poller.local = fake
    app.radio.open()
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    r = res[LOCAL_NODE]
    assert r.status == "OK", r.row
    assert (r.row["n_ok"], r.row["n_try"], r.row["med_us"], r.row["min_us"], r.row["max_us"]) == \
        (5, 7, 2400, 2390, 2410)
    assert r.row["fw"] == "gpio" and r.row["mac"] == "LOCAL" and abs(r.row["dist_cm"] - 41.21) < 0.1
    assert res[0].status == "OK"   # XBee の子機はこれまでどおり
    # 全部範囲外なら NO_ECHO
    fake.samples = [0] * 7
    assert app.poller.poll_local().status == "NO_ECHO"
    app.radio.close()


def test_local_gpio_open_failure_and_recovery(env, monkeypatch):
    monkeypatch.setattr(local_mod, "PING_GAP_S", 0)
    app = App(gpio_cfg(env))
    fake = FakeGpio([3000] * 7, fail_open=True)
    app.local = app.poller.local = fake
    app.radio.open()
    res = {r.node: r for r in app.poller.run_cycle(time.time())}
    assert res[LOCAL_NODE].status == "PORT_ERR" and "GPIO" in res[LOCAL_NODE].row["raw"]
    assert res[0].status == "OK" and app.poller.fatal is None
    fake.fail_open = False
    assert app.poller.poll_local().status == "OK" and app.poller.local_error is None
    app.radio.close()


def test_gpio_sensor_without_device_is_port_error():
    # 実機でない環境(gpiod がない、またはチップがない)では open が LocalSensorError になる
    s = local_mod.GpioSensor("/dev/no-such-gpiochip", 23, 24)
    with pytest.raises(local_mod.LocalSensorError):
        s.open()
    assert not s.is_open


# ---------------------------------------------------------------- 分担表(GAS が返す、親機 2 台)
def polled(res):
    return sorted(r.node for r in res)


def test_assign_initial_then_table_from_gas(env):
    tmp, gas, emu, _, _ = env
    cfg = make_cfg(tmp, emu.port, gas.url, {n: MACS[n] for n in range(7)},
                   extra="[assign]\ninitial = [0, 1]\n")
    app = App(cfg)
    app.radio.open()
    # 分担表を受け取るまでは initial だけ
    assert polled(app.poller.run_cycle(time.time())) == [0, 1]
    assert app.handle_control("status", {})["assign"]["source"] == "initial"
    # GAS が分担表を返す(他の親機の担当・知らないノード・壊れた値は自分では測らない)
    gas.assign = {"0": "parent-1", "5": "parent-1", "2": "parent-2", "99": "parent-1", "x": "parent-1"}
    app.uploader.upload_once()
    assert gas.requests[-1]["local_node"] is None
    assert polled(app.poller.run_cycle(time.time())) == [0, 5]
    st = app.handle_control("status", {})["assign"]
    assert st["source"] == "gas" and st["nodes"] == [0, 5] and st["received_ts"]
    # 形の壊れた応答は無視して、前の表を使い続ける
    gas.assign = ["not", "a", "dict"]
    app.uploader.upload_once()
    assert app.assignment.my_nodes() == [0, 5]
    app.radio.close()
    app.store.close()
    # 再起動しても、回線がなくても、最後に受け取った表で測る
    app2 = App(cfg)
    assert app2.assignment.my_nodes() == [0, 5]
    assert app2.handle_control("status", {})["assign"]["source"] == "gas"
    app2.radio.close()


def test_assign_never_moves_local_node(env, usb):
    tmp, gas, emu, _, _ = env
    dev = usb("v2")
    app = App(local_cfg(env, dev.port))
    app.radio.open()
    gas.assign = {"0": "parent-1", str(LOCAL_NODE): "parent-2"}   # 地点のノードが他の親機になっていても
    app.poller.run_cycle(time.time())   # 送るものがないと POST しない(= 分担表も来ない)ので、先に 1 サイクル
    app.uploader.upload_once()
    res = app.poller.run_cycle(time.time())
    assert polled(res) == [0, LOCAL_NODE]                        # 自分の地点は自分で測る
    assert gas.requests[-1]["local_node"] == LOCAL_NODE
    app.radio.close()
    app.local.close()


def test_assign_without_section_polls_all(env):
    tmp, gas, emu, cfg, _ = env
    app = App(cfg)
    app.radio.open()
    assert polled(app.poller.run_cycle(time.time())) == list(range(7))   # 従来どおり
    app.radio.close()


def test_assign_config_validation(env):
    tmp, gas, emu, _, _ = env
    with pytest.raises(config_mod.ConfigError, match="assign.initial"):
        make_cfg(tmp, emu.port, gas.url, {0: MACS[0]}, extra="[assign]\ninitial = [0, 9]\n")
    with pytest.raises(config_mod.ConfigError, match="assign.initial"):
        make_cfg(tmp, emu.port, gas.url, {0: MACS[0]}, extra='[assign]\ninitial = "0"\n')
