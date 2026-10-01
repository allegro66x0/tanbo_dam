"""テスト用 XBee コーディネータ・エミュレータ。

pty の片側で API フレーム(AP=1)を話し、digi-xbee をそのまま動かす。
子機の挙動はノードごとの ChildModel で再現する(実機の XBee 3 の動作を簡略化)。
"""
from __future__ import annotations

import os
import random
import threading
import tty
from dataclasses import dataclass, field
from typing import Callable, Optional

COORD_MAC = bytes.fromhex("0013A20041AAAAAA")

AT_VALUES = {
    b"AP": b"\x01", b"HV": b"\x42\x00", b"VR": b"\x10\x0D", b"SH": COORD_MAC[:4],
    b"SL": COORD_MAC[4:], b"MY": b"\x00\x00", b"NI": b"COORD", b"CE": b"\x01",
    b"AI": b"\x00", b"BR": b"\x01", b"ID": b"\x00" * 8, b"CH": b"\x0F",
}


def frame(data: bytes) -> bytes:
    return b"\x7e" + len(data).to_bytes(2, "big") + data + bytes([0xFF - (sum(data) & 0xFF)])


@dataclass
class ChildModel:
    """kind: v2 | v1 | dead | mute | late | garbage | noecho | flaky"""
    kind: str = "v2"
    echo_us: int = 3500
    delay: float = 0.15
    late_delay: float = 0.0
    requests: list = field(default_factory=list)


class XBeeEmulator:
    def __init__(self, children: dict[str, ChildModel]):
        self.children = {k.upper(): v for k, v in children.items()}
        self.master, self.slave = os.openpty()
        tty.setraw(self.master)
        self.port = os.ttyname(self.slave)
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self.at_log: list[bytes] = []
        self.dead = False   # True にすると一切応答しない(USB 抜け相当)
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()

    def close(self):
        self._stop.set()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def _write(self, data: bytes):
        with self._wlock:
            try:
                os.write(self.master, frame(data))
            except OSError:
                pass   # テスト終了後のタイマー

    def _later(self, dt: float, fn: Callable[[], None]):
        t = threading.Timer(dt, fn)
        t.daemon = True
        t.start()

    def send_rx(self, mac_hex: str, payload: bytes):
        mac = bytes.fromhex(mac_hex)
        self._write(b"\x90" + mac + b"\x12\x34" + b"\x01" + payload)

    def _reader(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            buf += chunk
            while True:
                i = buf.find(b"\x7e")
                if i < 0:
                    buf = b""
                    break
                buf = buf[i:]
                if len(buf) < 3:
                    break
                n = int.from_bytes(buf[1:3], "big")
                if len(buf) < 4 + n:
                    break
                data, cks = buf[3:3 + n], buf[3 + n]
                buf = buf[4 + n:]
                if (sum(data) + cks) & 0xFF != 0xFF:
                    continue
                if not self.dead:
                    self._handle(data)

    def _handle(self, d: bytes):
        ft = d[0]
        if ft in (0x08, 0x09):   # AT command / AT command queue
            fid, cmd, param = d[1], d[2:4], d[4:]
            self.at_log.append(cmd)
            if param:
                val, st = b"", 0
            else:
                val = AT_VALUES.get(cmd, b"\x00")
                st = 0
            self._write(b"\x88" + bytes([fid]) + cmd + bytes([st]) + val)
        elif ft == 0x10:  # Transmit request
            fid, dest, payload = d[1], d[2:10].hex().upper(), d[14:]
            child = self.children.get(dest)
            self._on_tx(fid, dest, payload, child)

    def _tx_status(self, fid: int, status: int, delay: float):
        if fid:
            self._later(delay, lambda: self._write(
                b"\x8b" + bytes([fid]) + b"\xff\xfe" + b"\x00" + bytes([status]) + b"\x00"))

    def _on_tx(self, fid, dest, payload: bytes, child: Optional[ChildModel]):
        if dest == COORD_MAC.hex().upper():
            # 実機(XBee3 コーディネータ)は自分宛ての送信をそのまま受信として折り返す
            self._later(0.05, lambda: self.send_rx(dest, payload))
            return
        if child is None or child.kind == "dead":
            self._tx_status(fid, 0x21, 0.3)   # Network ACK failure
            return
        child.requests.append(payload)
        self._tx_status(fid, 0x00, 0.03)
        text = payload.decode().strip()
        seq = int(text.split(",")[1]) if "," in text else -1
        k = child.kind
        if k == "flaky":
            k = random.choice(["v2", "v2", "mute"])
        if k == "mute":
            return
        if k == "v1":
            body = b"ID,%.1f,-999.0,-999.0,-999.0\n" % (child.echo_us * 0.0343 / 2)
        elif k == "garbage":
            body = b"\x00\xffzz\n"
        elif k == "noecho":
            body = b"D2,%d,0,7,-1,-1,-1,1234,2.0.0\n" % seq
        else:
            e = child.echo_us
            body = b"D2,%d,7,7,%d,%d,%d,1234,2.0.0\n" % (seq, e, e - 20, e + 30)
        delay = child.late_delay if k == "late" else child.delay
        if k == "late":
            body = b"D2,%d,7,7,%d,%d,%d,1234,2.0.0\n" % (seq, child.echo_us, child.echo_us,
                                                       child.echo_us)
        self._later(delay, lambda: self.send_rx(dest, body))
