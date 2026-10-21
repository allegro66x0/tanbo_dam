"""親機の地点のセンサ。

- LocalSensor: 親機に USB でつないだ ESP32 + HC-SR04。ESP32 には子機と同じファーム(tanbo_child_v2)を書く。
  USB シリアルに REQ,<seq> を送ると、デバッグ出力(ping の値など)に混じって D2 行が返る。行の解釈は poller が行う
- GpioSensor: 親機の GPIO に直接つないだ HC-SR04(par2)。子機のファームと同じ測り方を親機の中で行う
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Optional

log = logging.getLogger(__name__)

BOOT_WAIT_S = 2.5   # 開いた直後に ESP32 がリセットされても起動メッセージが出切る時間

# GpioSensor の測定パラメータ(子機ファーム tanbo_child_v2.ino と同じ値にそろえる)
N_TRY = 7
PING_GAP_S = 0.060
ECHO_TIMEOUT_S = 0.025    # TRIG からエコーの立ち下がりまでの待ち上限(pulseIn と同じ約 4.3 m 相当)
MIN_VALID_US = 290        # 約 5 cm 未満は残響として捨てる
MAX_VALID_US = 11650      # 約 2 m 超は捨てる


class LocalSensorError(RuntimeError):
    pass


def _io_errors() -> tuple[type[BaseException], ...]:
    """ポートの読み書きで起きうる例外。

    termios.error は OSError の仲間ではない。USB が抜けて挿し直されたあと、開いたままの
    古いポートへの tcflush で実機で発生した(Input/output error)。
    """
    import serial

    errs: list[type[BaseException]] = [serial.SerialException, OSError]
    try:
        import termios
        errs.append(termios.error)
    except ImportError:   # Windows
        pass
    return tuple(errs)


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
        except _io_errors() as e:
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
        if not self.is_open:
            raise LocalSensorError("not open")
        try:
            self._ser.reset_input_buffer()
            self._buf = b""
            self._ser.write(data)
            self._ser.flush()
        except _io_errors() as e:
            raise LocalSensorError(f"write failed: {e}") from e

    def readline(self, timeout: float) -> Optional[bytes]:
        """1行(前後の空白を除く)を返す。timeout 秒以内に行が揃わなければ None。"""
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
            except _io_errors() as e:
                raise LocalSensorError(f"read failed: {e}") from e
            self._buf += chunk
            if len(self._buf) > 4096:   # 改行の来ないゴミが続く場合
                self._buf = self._buf[-512:]


def summarize_echo(samples_us: list[int]) -> tuple[int, Optional[int], Optional[int], Optional[int]]:
    """有効範囲のエコー時間だけで (n_ok, 中央値, 最小, 最大) を返す。子機ファームと同じ計算(偶数個なら中央 2 つの平均)"""
    ok = sorted(us for us in samples_us if MIN_VALID_US <= us <= MAX_VALID_US)
    n = len(ok)
    if n == 0:
        return 0, None, None, None
    med = ok[n // 2] if n % 2 else (ok[n // 2 - 1] + ok[n // 2]) // 2
    return n, med, ok[0], ok[-1]


class GpioSensor:
    """親機の GPIO に直接つないだ HC-SR04。libgpiod(v2)のエッジイベントで ECHO のパルス幅を測る。

    立ち上がり・立ち下がりの時刻はカーネルが割り込み時に付ける(CLOCK_MONOTONIC)ので、
    Python 側の遅れはパルス幅に入らない。TRIG の High は Python の sleep で 10 µs 以上(実際は数十 µs)になるが、
    HC-SR04 は 10 µs 以上なら長くてもよい。
    """

    def __init__(self, chip: str, trig: int, echo: int):
        self.chip = chip
        self.trig = trig
        self.echo = echo
        self._req = None

    def open(self) -> None:
        try:
            import gpiod
            from gpiod.line import Clock, Direction, Edge, Value
        except ImportError as e:
            raise LocalSensorError(f"gpiod (libgpiod v2 の Python バインディング) がない: {e}") from e
        self.close()
        try:
            self._req = gpiod.request_lines(self.chip, consumer="tanbo", config={
                self.trig: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.echo: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH,
                                              event_clock=Clock.MONOTONIC),
            })
        except Exception as e:
            raise LocalSensorError(f"GPIO を開けない ({self.chip} trig={self.trig} echo={self.echo}): {e}") from e
        log.info("local sensor (gpio) open: %s trig=%d echo=%d", self.chip, self.trig, self.echo)

    def close(self) -> None:
        if self._req is not None:
            try:
                self._req.release()
            except Exception as e:
                log.warning("gpio release error: %s", e)
        self._req = None

    @property
    def is_open(self) -> bool:
        return self._req is not None

    def _drain(self) -> None:
        while self._req.wait_edge_events(timedelta(0)):
            self._req.read_edge_events()

    def ping_us(self) -> int:
        """1 回測る。エコーのパルス幅(µs)。時間内に立ち上がり・立ち下がりがそろわなければ 0。"""
        from gpiod.line import Value
        from gpiod import EdgeEvent

        if self._req is None:
            raise LocalSensorError("not open")
        try:
            self._drain()   # 前回の長いパルス(エコーなしだと約 38 ms High が続く)の立ち下がりを捨てる
            self._req.set_value(self.trig, Value.ACTIVE)
            time.sleep(0.00001)
            self._req.set_value(self.trig, Value.INACTIVE)
            deadline = time.monotonic() + ECHO_TIMEOUT_S
            rise_ns: Optional[int] = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._req.wait_edge_events(timedelta(seconds=remaining)):
                    return 0
                for ev in self._req.read_edge_events():
                    if ev.event_type == EdgeEvent.Type.RISING_EDGE:
                        rise_ns = ev.timestamp_ns
                    elif rise_ns is not None:
                        return int((ev.timestamp_ns - rise_ns) // 1000)
        except LocalSensorError:
            raise
        except Exception as e:
            raise LocalSensorError(f"gpio error: {e}") from e

    def burst(self) -> tuple[list[int], int, Optional[int], Optional[int], Optional[int]]:
        """子機と同じく PING_GAP_S おきに N_TRY 回測る。(全サンプル, n_ok, 中央値, 最小, 最大)"""
        samples = []
        for i in range(N_TRY):
            if i:
                time.sleep(PING_GAP_S)
            samples.append(self.ping_us())
        return (samples, *summarize_echo(samples))
