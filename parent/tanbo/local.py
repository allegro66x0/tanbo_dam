"""親機の地点のセンサ(親機に USB でつないだ ESP32 + HC-SR04)。

ESP32 には子機と同じファーム(tanbo_child_v2)を書く。USB シリアルに REQ,<seq> を送ると、
デバッグ出力(ping の値など)に混じって D2 行が返る。行の解釈は poller が行う。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

BOOT_WAIT_S = 2.5   # 開いた直後に ESP32 がリセットされても起動メッセージが出切る時間


class LocalSensorError(RuntimeError):
    pass


class LocalSensor:
    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._ser = None
        self._buf = b""

    def open(self) -> None:
        import serial

        self.close()
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.2
        # DTR/RTS は ESP32 基板の自動リセット回路につながっている。開くたびにリセットしないよう下げておく
        s.dtr = False
        s.rts = False
        try:
            s.open()
            # 基板によっては開いた瞬間にリセットされる。起動メッセージは読み捨てる
            time.sleep(BOOT_WAIT_S)
            s.reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            try:
                s.close()
            except Exception:
                pass
            raise LocalSensorError(f"open failed: {e}") from e
        self._ser = s
        self._buf = b""
        log.info("local sensor open: %s", self.port)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception as e:
                log.warning("local sensor close error: %s", e)
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def send(self, data: bytes) -> None:
        """溜まっている入力(前回の遅れた応答・デバッグ出力)を捨ててから送る。"""
        import serial

        if not self.is_open:
            raise LocalSensorError("not open")
        try:
            self._ser.reset_input_buffer()
            self._buf = b""
            self._ser.write(data)
            self._ser.flush()
        except (serial.SerialException, OSError) as e:
            raise LocalSensorError(f"write failed: {e}") from e

    def readline(self, timeout: float) -> Optional[bytes]:
        """1行(前後の空白を除く)を返す。timeout 秒以内に行が揃わなければ None。"""
        import serial

        if not self.is_open:
            raise LocalSensorError("not open")
        end = time.monotonic() + timeout
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line, self._buf = self._buf[:i], self._buf[i + 1:]
                return line.strip()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            try:
                self._ser.timeout = min(remaining, 0.2)
                chunk = self._ser.read(self._ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                raise LocalSensorError(f"read failed: {e}") from e
            self._buf += chunk
            if len(self._buf) > 4096:   # 改行の来ないゴミが続く場合
                self._buf = self._buf[-512:]
