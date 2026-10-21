"""研究室での XBee の設定と、2 台親機構成の確認テスト(par2 の parent/.venv で動かす)

親機用の XBee は番号の後ろから使う: 12 = 研究室用コーディネータ、10 = par2(ルータ)。
**研究室用コーディネータ(12)は現地に持ち込まない**(同じ PAN ID のネットワークがもう 1 つできる)。

  python xbee_lab.py ports
  python xbee_lab.py info   --port P
  python xbee_lab.py setup  --port P --role coordinator|router [--pan 1114]
  python xbee_lab.py remote --port P --mac <子機の MAC>        # 子機 XBee の設定を無線越しに読む
  python xbee_lab.py remote-set --port P --mac <子機の MAC> AP=01   # 無線越しに書く(WR・AC まで)
  python xbee_lab.py test   --coord P1 --router P2 --mac <子機の MAC> [--count 3]
  python xbee_lab.py req    --port P --mac <子機の MAC> [--count 3]  # 1 台から送って受ける
  python xbee_lab.py child-setup --port P [--ni 名前] [--dry-run]   # USB アダプタに挿した XBee を子機用に設定する

child-setup: ID=1114、CE=0(ルータ)、JV=0、AP=1(API)、AO=0(受信は 0x90)、DH/DL=0、BD=3(9600)、NI=node<番号> に書き換えて保存する。
今が透過モード(AP=0)でも API モードでも書ける(透過なら +++ のコマンドモード、API なら AT フレーム)。
AP=1 の子機は、ESP32 のファームが v2.2.0 以降でないと応答できない。

test で確かめること:
  1. コーディネータから REQ → 応答がコーディネータに届く(基準)
  2. ルータ(par2)から REQ → 配送は成功するが、応答はコーディネータ側に届く(予想)。par2 には来ない
req は、コーディネータを抜いたあとにルータから送って、コーディネータなしでも配送できるかを見る。
"""
import argparse
import glob
import threading
import time

from digi.xbee.devices import RemoteXBeeDevice, XBeeDevice
from digi.xbee.exception import TransmitException, XBeeException
from digi.xbee.models.address import XBee64BitAddress

BAUD = 9600
LOCAL_PARAMS = ["NI", "CE", "AP", "ID", "OP", "OI", "CH", "AI", "JV", "NJ", "DH", "DL", "BD", "VR"]
REMOTE_PARAMS = ["NI", "CE", "AP", "AO", "ID", "OP", "CH", "AI", "DH", "DL", "JV"]

# 子機用の設定(AT コマンド名, 値の 16 進文字列)。AP は最後に書く(透過モードのコマンドモード中に API へ切り替わらないように)
# AO=0: 受信を 0x90(Receive Packet)で出す。AO=1 だと 0x91(Explicit)になり、v2.2.0 のファームが読まなかった(node 0、2026-10-21)
# JV=0: 起動時にコーディネータを探さず、覚えているネットワークに戻る。JV=1 だとコーディネータ(p3)が止まっている間に
#       電源が入り直した子機がネットワークを抜けて戻れなかった(2026-10-21 に研究室で確認)
CHILD_SETTINGS = [("ID", "1114"), ("CE", "0"), ("JV", "0"), ("DH", "0"), ("DL", "0"), ("BD", "3"), ("AO", "0"), ("AP", "1")]
CHILD_READ = ["SH", "SL", "NI", "CE", "AP", "AO", "ID", "JV", "DH", "DL", "BD", "VR"]

# ノード番号と XBee の 64bit アドレス(tanbo_moc/xbee_ID.xlsx)
NODE_MACS = {
    0: "0013A200423ECC65", 1: "0013A200423ECBC6", 2: "0013A200423EC0CD", 3: "0013A200423EB483",
    4: "0013A200423E9827", 5: "0013A200423EB80C", 6: "0013A200423EB0A3", 7: "0013A200423EB488",
    8: "0013A200423EC984", 9: "0013A200423EB6B4", 10: "0013A200423EAFE2", 11: "0013A200423EB740",
    12: "0013A200423EC8E7",
}
PARENT_XBEES = {10: "par2(ルータ)", 11: "p3(コーディネータ)", 12: "研究室用コーディネータ"}


def fmt(v: bytes | None) -> str:
    if v is None:
        return "-"
    if all(32 <= b < 127 for b in v) and v:
        return f"'{v.decode()}'"
    return v.hex().upper() or "''"


def open_dev(port: str) -> XBeeDevice:
    d = XBeeDevice(port, BAUD)
    d.open()
    d.set_sync_ops_timeout(5)
    return d


def read_params(get, names) -> dict:
    out = {}
    for n in names:
        try:
            out[n] = fmt(get(n))
        except Exception as e:   # 機種によって無いパラメータもある
            out[n] = f"({type(e).__name__})"
    return out


def cmd_ports(a):
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        print(p)


def cmd_info(a):
    d = open_dev(a.port)
    try:
        print("MAC", str(d.get_64bit_addr()), "mode", d.operating_mode)
        for k, v in read_params(d.get_parameter, LOCAL_PARAMS).items():
            print(f"  {k} = {v}")
    finally:
        d.close()


def cmd_setup(a):
    d = open_dev(a.port)
    try:
        print("MAC", str(d.get_64bit_addr()))
        print("before", read_params(d.get_parameter, ["CE", "AP", "ID", "JV"]))
        d.set_parameter("ID", bytes.fromhex(a.pan.rjust(16, "0")))
        d.set_parameter("CE", b"\x01" if a.role == "coordinator" else b"\x00")
        if a.role == "router":
            d.set_parameter("JV", b"\x00")   # コーディネータが止まっている間に再起動しても、覚えているネットワークに戻る
        d.set_parameter("AP", b"\x01")       # 親機側は API モード(tanbo と digi-xbee が前提)
        d.write_changes()
        d.apply_changes()
        print("after ", read_params(d.get_parameter, ["CE", "AP", "ID", "JV"]))
        print("ネットワークの形成/参加に数十秒かかる。info で AI = 00 になるまで待つ")
    finally:
        d.close()


def cmd_remote(a):
    d = open_dev(a.port)
    try:
        r = RemoteXBeeDevice(d, XBee64BitAddress.from_hex_string(a.mac))
        for k, v in read_params(r.get_parameter, REMOTE_PARAMS).items():
            print(f"  {k} = {v}")
    finally:
        d.close()


def cmd_remote_set(a):
    """子機 XBee のパラメータを無線越しに書く(例 AP=01)。WR で保存し、AC で反映する。
    コーディネータがいる状態で使う(コーディネータ不在のルータからは通らなかった、2026-10-21)"""
    d = open_dev(a.port)
    try:
        r = RemoteXBeeDevice(d, XBee64BitAddress.from_hex_string(a.mac))
        names = [kv.split("=", 1)[0].upper() for kv in a.params]
        print("before", read_params(r.get_parameter, names))
        for kv in a.params:
            k, v = kv.split("=", 1)
            r.set_parameter(k.upper(), bytes.fromhex(v))
        r.write_changes()
        r.apply_changes()
        time.sleep(1)
        print("after ", read_params(r.get_parameter, names))
    finally:
        d.close()


# ---- 子機用の設定(child-setup) ---------------------------------------------------

def node_of(mac: str):
    return next((n for n, m in NODE_MACS.items() if m == mac.upper()), None)


def at_open(port: str):
    """透過モードの XBee を +++ でコマンドモードにする。応答したボーレートで開いた serial を返す。"""
    import serial

    for baud in (9600, 115200, 57600, 38400, 19200):
        s = serial.Serial(port, baud, timeout=1.5)
        time.sleep(1.2)                 # ガードタイム(GT 既定 1 秒)の前の無通信
        s.reset_input_buffer()
        s.write(b"+++")
        time.sleep(1.2)                 # ガードタイムの後の無通信
        if b"OK" in s.read(16):
            return s, baud
        s.close()
    return None, None


def at(s, cmd: str) -> str:
    s.write(f"AT{cmd}\r".encode())
    return s.read_until(b"\r").decode(errors="replace").strip()


def cmd_child_setup(a):
    # 1) API モードとして開けるか(digi-xbee)
    try:
        d = open_dev(a.port)
    except Exception:
        d = None
    if d is not None:
        try:
            mac = str(d.get_64bit_addr())
            n = node_of(mac)
            ni = a.ni or (f"node{n}" if n is not None else None)
            print(f"API モードで開けた: MAC {mac}" + (f"(node {n})" if n is not None else "(表にない XBee)"))
            if n in PARENT_XBEES:
                print(f"注意: これは親機用の XBee({PARENT_XBEES[n]})。子機用に書き換えない")
                return
            before = read_params(d.get_parameter, [k for k, _ in CHILD_SETTINGS] + ["NI"])
            print("before", before)
            done = all(before[k] != "-" and int(before[k], 16) == int(v, 16) for k, v in CHILD_SETTINGS
                       if not before[k].startswith("(")) and (not ni or before["NI"] == f"'{ni}'")
            if done:
                print("すでに子機用の設定になっている。書き換えない")
                return
            if a.dry_run:
                return
            for k, v in CHILD_SETTINGS:
                d.set_parameter(k, bytes.fromhex(v.rjust(len(v) + len(v) % 2, "0")))
            if ni:
                d.set_parameter("NI", ni.encode())
            d.write_changes()
            d.apply_changes()
            time.sleep(1)
            print("after ", read_params(d.get_parameter, [k for k, _ in CHILD_SETTINGS] + ["NI"]))
        finally:
            d.close()
        return

    # 2) 透過モード: +++ でコマンドモード。digi-xbee が判定のために +++ を送っていることがあるので、
    #    そのコマンドモードが時間切れ(CT 既定 10 秒)で終わるのを待ってから
    print("API モードでは開けない → 透過モードとして +++ で入る(11 秒待つ)")
    time.sleep(11)
    s, baud = at_open(a.port)
    if s is None:
        print("XBee が応答しない(API でも +++ でも)。挿し方・電源・アダプタを確認")
        return
    try:
        before = {k: at(s, k) for k in CHILD_READ}
        mac = (before["SH"].rjust(8, "0") + before["SL"].rjust(8, "0")).upper()
        n = node_of(mac)
        ni = a.ni or (f"node{n}" if n is not None else None)
        print(f"透過モード(コマンドモード、{baud} bps)で開けた: MAC {mac}" +
              (f"(node {n})" if n is not None else "(表にない XBee)"))
        print("before", {k: v for k, v in before.items() if k not in ("SH", "SL")})
        if n in PARENT_XBEES:
            print(f"注意: これは親機用の XBee({PARENT_XBEES[n]})。子機用に書き換えない")
            at(s, "CN")
            return
        if a.dry_run:
            at(s, "CN")
            return
        results = [(k, v, at(s, k + v)) for k, v in CHILD_SETTINGS]
        if ni:
            results.append(("NI", ni, at(s, "NI" + ni)))
        bad = [r for r in results if r[2] != "OK"]
        if bad:
            print("書けなかった設定があるので保存しない:", bad)
            at(s, "CN")
            return
        print("WR(保存):", at(s, "WR"), " CN(反映・終了):", at(s, "CN"))
    finally:
        s.close()
    # 3) AP=1・9600 bps になったので API で開いて確かめる
    time.sleep(1.5)
    d = open_dev(a.port)
    try:
        print("after ", read_params(d.get_parameter, [k for k, _ in CHILD_SETTINGS] + ["NI"]))
    finally:
        d.close()


class Side:
    """1 台の XBee の受信をすべて記録する。"""

    def __init__(self, name: str, port: str):
        self.name = name
        self.dev = open_dev(port)
        self.mac = str(self.dev.get_64bit_addr())
        self.rx: list[tuple[float, str, bytes]] = []
        self.lock = threading.Lock()
        self.dev.add_data_received_callback(self._on)

    def _on(self, msg):
        src = str(msg.remote_device.get_64bit_addr())
        with self.lock:
            self.rx.append((time.monotonic(), src, bytes(msg.data)))
        print(f"    [{self.name} 受信] from {src}: {bytes(msg.data)!r}")

    def send_req(self, mac: str, seq: int) -> str:
        r = RemoteXBeeDevice(self.dev, XBee64BitAddress.from_hex_string(mac))
        try:
            st = self.dev.send_data(r, f"REQ,{seq}\n".encode())
            return f"配送成功 ({st.transmit_status.description})" if st else "配送成功"
        except TransmitException as e:
            return f"配送失敗: {e}"
        except XBeeException as e:
            return f"送信エラー: {e}"

    def got(self, seq: int, since: float, mac: str) -> bool:
        """REQ のあとに子機から応答が来たか。v2 は seq まで一致、v1(ID<n>,...)は seq がないので届いたかだけ"""
        with self.lock:
            return any(t >= since and src.upper() == mac.upper()
                       and (data.startswith(b"D2,%d," % seq) or data.startswith(b"ID"))
                       for t, src, data in self.rx)

    def close(self):
        self.dev.close()


def run_round(sender: Side, sides: list[Side], mac: str, seq: int, wait: float) -> dict:
    t0 = time.monotonic()
    print(f"  {sender.name} → REQ,{seq}: {sender.send_req(mac, seq)}")
    time.sleep(wait)
    return {s.name: s.got(seq, t0, mac) for s in sides}


def cmd_test(a):
    coord = Side("coordinator", a.coord)
    router = Side("router(par2)", a.router)
    sides = [coord, router]
    print(f"coordinator MAC {coord.mac} / router MAC {router.mac} / 子機 {a.mac}")
    seq = int(time.time()) % 50000
    rows = []
    try:
        for sender in (coord, router):
            print(f"--- {sender.name} から送る")
            for _ in range(a.count):
                seq += 1
                got = run_round(sender, sides, a.mac, seq, a.wait)
                rows.append((sender.name, seq, got))
                time.sleep(0.5)
    finally:
        for s in sides:
            s.close()
    print("\n=== 結果(子機の応答がどちらに届いたか)")
    for name, seq, got in rows:
        print(f"  送信 {name:13s} seq {seq}: " + ", ".join(f"{k}={'届いた' if v else '-'}" for k, v in got.items()))


def cmd_req(a):
    s = Side("this", a.port)
    print(f"MAC {s.mac} / 子機 {a.mac}")
    seq = int(time.time()) % 50000
    try:
        for _ in range(a.count):
            seq += 1
            got = run_round(s, [s], a.mac, seq, a.wait)
            print(f"  seq {seq}: 応答 {'届いた' if got['this'] else 'なし'}")
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports")
    p = sub.add_parser("info"); p.add_argument("--port", required=True)
    p = sub.add_parser("setup"); p.add_argument("--port", required=True)
    p.add_argument("--role", choices=["coordinator", "router"], required=True)
    p.add_argument("--pan", default="1114", help="PAN ID(16 進、既定は現地と同じ 1114)")
    p = sub.add_parser("remote"); p.add_argument("--port", required=True); p.add_argument("--mac", required=True)
    p = sub.add_parser("remote-set"); p.add_argument("--port", required=True); p.add_argument("--mac", required=True)
    p.add_argument("params", nargs="+", help="NAME=16進値(例 AP=01)")
    p = sub.add_parser("child-setup"); p.add_argument("--port", required=True)
    p.add_argument("--ni", help="NI(ノードの名前)。省略時は node<番号>")
    p.add_argument("--dry-run", action="store_true", help="今の設定を読むだけで書かない")
    for name in ("test", "req"):
        p = sub.add_parser(name)
        if name == "test":
            p.add_argument("--coord", required=True); p.add_argument("--router", required=True)
        else:
            p.add_argument("--port", required=True)
        p.add_argument("--mac", required=True)
        p.add_argument("--count", type=int, default=3)
        p.add_argument("--wait", type=float, default=3.0, help="1 回の REQ のあと応答を待つ秒数")
    a = ap.parse_args()
    {"ports": cmd_ports, "info": cmd_info, "setup": cmd_setup, "remote": cmd_remote,
     "remote-set": cmd_remote_set, "child-setup": cmd_child_setup,
     "test": cmd_test, "req": cmd_req}[a.cmd](a)


if __name__ == "__main__":
    main()
