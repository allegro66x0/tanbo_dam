"""親機 XBee(コーディネータ、API モード)へのアクセス層。

受信フレームと Transmit Status はすべてイベントキューに積み、
送信は非同期で行う(Transmit Status を待ってブロックしない)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class RxEvent:
    mac: str
    data: bytes
    t: float          # time.monotonic()


@dataclass
class TxStatusEvent:
    frame_id: int
    status: int       # 0x00 = 成功
    retries: int
    t: float


class RadioError(RuntimeError):
    pass


class XBeeRadio:
    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self.events: "queue.Queue[RxEvent | TxStatusEvent]" = queue.Queue(maxsize=1000)
        self._dev = None
        self._lock = threading.Lock()
        self.local_mac: Optional[str] = None

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        from digi.xbee.devices import XBeeDevice
        from digi.xbee.models.mode import OperatingMode

        with self._lock:
            self._close_locked()
            dev = XBeeDevice(self.port, self.baud)
            try:
                dev.open()
                if dev.operating_mode != OperatingMode.API_MODE:
                    raise RadioError(f"コーディネータは AP=1 が必要 (現在 {dev.operating_mode})")
                dev.add_packet_received_callback(self._on_packet)
            except Exception:
                try:
                    if dev.is_open():
                        dev.close()
                except Exception:
                    pass
                raise
            self._dev = dev
            self.local_mac = str(dev.get_64bit_addr()).upper()
            log.info("XBee open: %s MAC=%s", self.port, self.local_mac)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._dev is not None:
            try:
                if self._dev.is_open():
                    self._dev.close()
            except Exception as e:
                log.warning("XBee close error: %s", e)
        self._dev = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None and self._dev.is_open()

    # ---- I/O -------------------------------------------------------------
    def send(self, mac: str, data: bytes) -> int:
        """ユニキャスト送信。frame_id を返す(Transmit Status はイベントで届く)。"""
        from digi.xbee.models.address import XBee16BitAddress, XBee64BitAddress
        from digi.xbee.packets.common import TransmitPacket

        with self._lock:
            dev = self._dev
            if dev is None or not dev.is_open():
                raise RadioError("XBee not open")
            fid = dev.get_next_frame_id()
            pkt = TransmitPacket(
                fid, XBee64BitAddress.from_hex_string(mac), XBee16BitAddress.UNKNOWN_ADDRESS,
                0, 0, rf_data=data, op_mode=dev.operating_mode)
            try:
                dev.send_packet(pkt, sync=False)
            except Exception as e:
                raise RadioError(f"send failed: {e}") from e
            return fid

    def ping(self) -> Optional[int]:
        """シリアルリンクの生存確認。AI(Association Indication)を返す。"""
        with self._lock:
            dev = self._dev
            if dev is None or not dev.is_open():
                raise RadioError("XBee not open")
            try:
                v = dev.get_parameter("AI")
            except Exception as e:
                raise RadioError(f"AT AI failed: {e}") from e
            return int.from_bytes(v, "big") if v else None

    def _on_packet(self, packet) -> None:
        # digi-xbee の受信スレッドから呼ばれる。重い処理はしない。
        from digi.xbee.packets.aft import ApiFrameType
        try:
            ft = packet.get_frame_type()
            now = time.monotonic()
            if ft in (ApiFrameType.RECEIVE_PACKET, ApiFrameType.EXPLICIT_RX_INDICATOR):
                ev = RxEvent(str(packet.x64bit_source_addr).upper(), bytes(packet.rf_data or b""), now)
            elif ft == ApiFrameType.TRANSMIT_STATUS:
                ev = TxStatusEvent(packet.frame_id, packet.transmit_status.code,
                                   packet.transmit_retry_count, now)
            else:
                return
            self.events.put_nowait(ev)
        except queue.Full:
            log.error("radio event queue full; dropping")
        except Exception:
            log.exception("packet callback error")
