"""子機の単体・通信試験(ラップトップを親機代わりにする)

親機 XBee(API モード AP=1、BD=9600)を USB でつなぎ、ブロードキャストで REQ,<seq> を送る。
子機 ESP32 の USB シリアルも同時に読み、どこで止まっているかを切り分ける。

  python link_test.py --xbee COM11 --child COM12 [--count 5]

見るところ:
  TxStatus        親機 XBee → 子機 XBee の無線配送(ブロードキャストは常に 0x00 なので参考程度)
  child log [REQ] 子機 ESP32 が XBee から REQ を受け取れたか(XBee DOUT → GPIO25 の配線)
  RX from ...     子機の応答が親機まで戻ったか(GPIO26 → XBee DIN の配線)
  ping ... us     HC-SR04 のエコー(0 ばかりなら TRIG/ECHO 配線・分圧・電源)
"""
import argparse
import threading
import time

import serial


def api_frame(data: bytes) -> bytes:
    return b"\x7e" + len(data).to_bytes(2, "big") + data + bytes([0xFF - (sum(data) & 0xFF)])


def tx_request(frame_id: int, payload: bytes, dest64: bytes = b"\x00" * 6 + b"\xff\xff") -> bytes:
    # 0x10 Transmit Request: frame_id, 64bit 宛先, 16bit 宛先 0xFFFE, radius 0, options 0
    return api_frame(bytes([0x10, frame_id]) + dest64 + b"\xff\xfe\x00\x00" + payload)


def read_frames(ser: serial.Serial, stop: threading.Event):
    buf = b""
    while not stop.is_set():
        buf += ser.read(256)
        while True:
            i = buf.find(b"\x7e")
            if i < 0:
                buf = b""
                break
            buf = buf[i:]
            if len(buf) < 3:
                break
            n = int.from_bytes(buf[1:3], "big")
            if len(buf) < n + 4:
                break
            data, cks = buf[3:3 + n], buf[3 + n]
            buf = buf[n + 4:]
            if (sum(data) + cks) & 0xFF != 0xFF:
                print("[xbee] checksum NG", data.hex())
                continue
            show_frame(data)


def show_frame(d: bytes):
    t = d[0]
    if t == 0x8B:
        print(f"[xbee] TxStatus id={d[1]} retry={d[4]} status=0x{d[5]:02x} discovery=0x{d[6]:02x}")
    elif t == 0x90:
        src = d[1:9].hex()
        print(f"[xbee] RX from {src}: {d[12:]!r}")
    elif t == 0x8A:
        print(f"[xbee] ModemStatus 0x{d[1]:02x}")
    else:
        print(f"[xbee] frame 0x{t:02x} {d.hex()}")


def read_child(ser: serial.Serial, stop: threading.Event):
    while not stop.is_set():
        line = ser.readline()
        if line:
            print("[child]", line.decode("utf-8", "replace").rstrip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xbee", required=True, help="親機 XBee の COM ポート")
    ap.add_argument("--child", help="子機 ESP32 の COM ポート(省略可)")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args()

    stop = threading.Event()
    xb = serial.Serial(a.xbee, 9600, timeout=0.1)
    threading.Thread(target=read_frames, args=(xb, stop), daemon=True).start()
    if a.child:
        ch = serial.Serial()
        ch.port, ch.baudrate, ch.timeout = a.child, 115200, 0.1
        ch.dtr = ch.rts = False   # 開いただけで ESP32 がリセットされないように
        ch.open()
        threading.Thread(target=read_child, args=(ch, stop), daemon=True).start()

    for seq in range(1, a.count + 1):
        print(f"--- send REQ,{seq} (broadcast)")
        xb.write(tx_request(seq, f"REQ,{seq}\n".encode()))
        time.sleep(a.interval)
    time.sleep(1)
    stop.set()


if __name__ == "__main__":
    main()
