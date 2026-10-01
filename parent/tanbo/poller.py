"""計測スケジューラとポーリング。

- 周期は壁時計の境界(interval の倍数 + offset)に揃える
- 一時的な周期の上書き(設置チェック用)は期限付き。期限が来たら通常周期に戻る
- 1サイクル = 全ノードへ順に REQ,<seq> を送り、seq が一致した応答だけ採用
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import protocol, sysinfo
from .config import Config
from .radio import RadioError, RxEvent, TxStatusEvent
from .store import Store

log = logging.getLogger(__name__)

RADIO_FATAL_AFTER_S = 1800   # XBee が使えない状態がこれ以上続いたらプロセスを終了(systemd が再起動)


class Schedule:
    def __init__(self, base_interval: int, offset: int = 0):
        self.base_interval = base_interval
        self.offset = offset
        self._override: Optional[tuple[int, float]] = None   # (interval, until_wall)
        self._lock = threading.Lock()

    def set_override(self, interval: int, duration_s: float) -> float:
        until = time.time() + duration_s
        with self._lock:
            self._override = (interval, until)
        return until

    def clear_override(self) -> None:
        with self._lock:
            self._override = None

    def current(self, now: Optional[float] = None) -> tuple[int, Optional[float]]:
        now = time.time() if now is None else now
        with self._lock:
            if self._override and now < self._override[1]:
                return self._override
            self._override = None
            return self.base_interval, None

    def next_slot(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        interval, until = self.current(now)
        slot = (math.floor((now - self.offset) / interval) + 1) * interval + self.offset
        if until is not None and slot > until:
            # 上書きの期限までに次の枠がない → 通常周期の次の境界へ
            base = self.base_interval
            slot = (math.floor((now - self.offset) / base) + 1) * base + self.offset
        return slot


@dataclass
class PollResult:
    node: int
    mac: str
    status: str
    row: dict[str, Any] = field(default_factory=dict)


def _ranges(nums: list[int]) -> str:
    """[1, 3, 4, 5, 12] -> '1,3-5,12'"""
    out: list[str] = []
    nums = sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def summarize_failures(results: list["PollResult"]) -> str:
    """OK 以外のノードを status(TX_FAIL は配送コード別)ごとにまとめる。

    例: 'TX_FAIL(0x24): 1,3-10,12; TIMEOUT: 2'
    """
    groups: dict[str, list[int]] = {}
    for r in results:
        if r.status in ("OK", "OK_V1"):
            continue
        key = r.status
        tx = r.row.get("tx_status")
        if r.status == "TX_FAIL" and tx is not None:
            key = f"TX_FAIL(0x{tx:02X})"
        groups.setdefault(key, []).append(r.node)
    return "; ".join(f"{k}: {_ranges(v)}" for k, v in groups.items())


class Poller(threading.Thread):
    def __init__(self, cfg: Config, radio, store: Store,
                 health_extra: Optional[Callable[[], dict[str, Any]]] = None):
        super().__init__(name="poller", daemon=True)
        self.cfg = cfg
        self.radio = radio
        self.store = store
        self.schedule = Schedule(cfg.schedule.interval_s, cfg.schedule.offset_s)
        self.health_extra = health_extra or (lambda: {})
        self.mac_to_node = cfg.mac_to_node()
        self.commands: "queue.Queue[tuple[str, Any, queue.Queue]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.heartbeat = time.monotonic()
        self.fatal: Optional[str] = None
        self.last_cycle: dict[str, Any] = {}
        self.next_slot: float = self.schedule.next_slot()
        self._seq = int(time.time()) & 0xFFFF
        self._radio_bad_since: Optional[float] = None
        self._radio_retry_at = 0.0
        self._unknown_mac_logged: dict[str, float] = {}
        self._self_node_logged: set[int] = set()
        self._last_fails = ""
        self._boot_id = sysinfo.boot_id()

    # ---- radio management -------------------------------------------------
    def _ensure_radio(self) -> bool:
        if self.radio.is_open:
            return True
        now = time.monotonic()
        if now < self._radio_retry_at:
            return False
        try:
            self.radio.open()
            self._radio_bad_since = None
            return True
        except Exception as e:
            log.error("XBee open failed: %s", e)
            self._radio_failed()
            return False

    def _radio_failed(self) -> None:
        now = time.monotonic()
        if self._radio_bad_since is None:
            self._radio_bad_since = now
        self._radio_retry_at = now + 30
        try:
            self.radio.close()
        except Exception:
            pass
        if now - self._radio_bad_since > RADIO_FATAL_AFTER_S:
            self.fatal = f"XBee unavailable for >{RADIO_FATAL_AFTER_S}s"

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self.radio.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(ev, RxEvent):
                self._log_stray(ev, "stale")

    def _log_stray(self, ev: RxEvent, why: str) -> None:
        node = self.mac_to_node.get(ev.mac)
        if node is None:
            last = self._unknown_mac_logged.get(ev.mac, 0)
            if time.monotonic() - last > 3600:
                log.warning("frame from unknown MAC %s: %r", ev.mac, ev.data[:60])
                self._unknown_mac_logged[ev.mac] = time.monotonic()
        else:
            log.info("%s reply from node %d ignored: %r", why, node, ev.data[:60])

    # ---- one node -----------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def poll_one(self, node: int, mac: str) -> PollResult:
        seq = self._next_seq()
        row: dict[str, Any] = {"node": node, "mac": mac, "seq": seq}
        t0 = time.monotonic()
        try:
            fid = self.radio.send(mac, protocol.build_req(seq))
        except RadioError as e:
            log.error("send to node %d failed: %s", node, e)
            self._radio_failed()
            row.update(status="RADIO_ERR", raw=str(e)[:200])
            return PollResult(node, mac, "RADIO_ERR", row)

        deadline = t0 + self.cfg.xbee.reply_timeout_s
        tx_status: Optional[int] = None
        status = "TIMEOUT"
        while True:
            self.heartbeat = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ev = self.radio.events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if isinstance(ev, TxStatusEvent):
                if ev.frame_id == fid:
                    tx_status = ev.status
                    if ev.status != 0:
                        status = "TX_FAIL"
                        break
                continue
            if ev.mac != mac:
                self._log_stray(ev, "out-of-turn")
                continue
            try:
                rep = protocol.parse_reply(ev.data)
            except protocol.ParseError as e:
                log.warning("node %d bad reply: %s", node, e)
                status = "BAD_REPLY"
                row["raw"] = ev.data[:200].decode("ascii", "replace")
                row["rtt_ms"] = int((ev.t - t0) * 1000)
                break
            if rep.version == 2 and rep.seq != seq:
                log.info("node %d late reply seq=%s (want %d) ignored", node, rep.seq, seq)
                continue
            row["rtt_ms"] = int((ev.t - t0) * 1000)
            if rep.version == 2:
                status = "OK" if rep.n_ok else "NO_ECHO"
                row.update(n_ok=rep.n_ok, n_try=rep.n_try, med_us=rep.med_us,
                           min_us=rep.min_us, max_us=rep.max_us, dist_cm=rep.dist_cm,
                           child_uptime_s=rep.child_uptime_s, fw=rep.fw)
            else:
                status = "OK_V1" if rep.dist_cm is not None else "NO_DATA"
                row.update(dist_cm=rep.dist_cm, seq=None, fw="v1")
            break

        row["status"] = status
        row["tx_status"] = tx_status
        row["ts"] = time.time()
        return PollResult(node, mac, status, row)

    # ---- cycle ------------------------------------------------------------
    def run_cycle(self, cycle_ts: float) -> list[PollResult]:
        t_start = time.monotonic()
        synced = sysinfo.clock_synced()
        results: list[PollResult] = []
        xbee_ai: Optional[int] = None

        radio_ok = self._ensure_radio()
        if radio_ok:
            try:
                xbee_ai = self.radio.ping()
            except RadioError as e:
                log.error("XBee ping failed: %s", e)
                self._radio_failed()
                radio_ok = False
        if radio_ok:
            self._drain_events()

        for node, mac in self.cfg.nodes.items():
            if self.stop_event.is_set():
                break
            if radio_ok and mac == self.radio.local_mac:
                # 自分宛ての REQ はそのまま折り返ってきて BAD_REPLY になる(実機で確認)
                if node not in self._self_node_logged:
                    log.warning("node %d (%s) は親機 XBee 自身のアドレス。計測対象から外す",
                                node, mac)
                    self._self_node_logged.add(node)
                continue
            self.heartbeat = time.monotonic()
            if radio_ok and self.radio.is_open:
                res = self.poll_one(node, mac)
                time.sleep(self.cfg.xbee.gap_s)
            else:
                res = PollResult(node, mac, "RADIO_ERR",
                                 {"node": node, "mac": mac, "status": "RADIO_ERR",
                                  "ts": time.time()})
            results.append(res)

        for r in results:
            r.row.update(cycle_ts=cycle_ts, synced=None if synced is None else int(synced),
                         boot_id=self._boot_id)
        self.store.add_measurements(r.row for r in results)

        cycle_ms = int((time.monotonic() - t_start) * 1000)
        n_ok = sum(r.status in ("OK", "OK_V1") for r in results)
        interval, until = self.schedule.current()
        self.last_cycle = {"cycle_ts": cycle_ts, "cycle_ms": cycle_ms, "n_ok": n_ok,
                           "n_nodes": len(results),
                           "status": {r.node: r.status for r in results}}
        fails = summarize_failures(results)
        # 失敗の内訳が前回から変わったときだけ WARNING(同じ状態が続く間は INFO)
        level = logging.WARNING if fails and fails != self._last_fails else logging.INFO
        self._last_fails = fails
        log.log(level, "cycle %s: %d/%d OK in %d ms%s", time.strftime("%H:%M:%S",
                time.localtime(cycle_ts)), n_ok, len(results), cycle_ms,
                f"; {fails}" if fails else "")

        h = {"ts": time.time(), "synced": None if synced is None else int(synced),
             "boot_id": self._boot_id, "interval_s": interval, "cycle_ms": cycle_ms,
             "n_ok": n_ok, "n_nodes": len(results), "xbee_ai": xbee_ai,
             "uptime_s": sysinfo.uptime_s(), "cpu_temp": sysinfo.cpu_temp(),
             "load1": sysinfo.load1(), "mem_avail_mb": sysinfo.mem_avail_mb()}
        try:
            h.update(self.health_extra())
        except Exception:
            log.exception("health_extra failed")
        self.store.add_health(h)
        return results

    # ---- control ------------------------------------------------------------
    def submit(self, cmd: str, arg: Any = None, timeout: float = 120) -> Any:
        """制御スレッドから呼ぶ。ポーラースレッドで実行して結果を返す。"""
        rq: queue.Queue = queue.Queue(maxsize=1)
        self.commands.put((cmd, arg, rq))
        return rq.get(timeout=timeout)

    def _handle_command(self, cmd: str, arg: Any) -> Any:
        if cmd == "poll":
            res = self.run_cycle(time.time())
            return {r.node: r.status for r in res}
        if cmd == "get":
            node = int(arg)
            mac = self.cfg.nodes.get(node)
            if mac is None:
                return {"error": f"node {node} は設定にありません"}
            if not self._ensure_radio():
                return {"error": "XBee が使えません"}
            self._drain_events()
            return self.poll_one(node, mac).row   # 手動取得は記録しない
        if cmd == "noop":
            return None
        return {"error": f"unknown command {cmd}"}

    # ---- main loop ----------------------------------------------------------
    def run(self) -> None:
        log.info("poller start: nodes=%s interval=%ds", list(self.cfg.nodes),
                 self.schedule.base_interval)
        self._ensure_radio()
        self.next_slot = self.schedule.next_slot()
        while not self.stop_event.is_set():
            self.heartbeat = time.monotonic()
            try:
                cmd, arg, rq = self.commands.get(timeout=0.5)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    rq.put(self._handle_command(cmd, arg))
                except Exception as e:
                    log.exception("command %s failed", cmd)
                    rq.put({"error": str(e)})
                self.next_slot = self.schedule.next_slot()
                continue

            now = time.time()
            interval = self.schedule.current(now)[0]
            if now - self.next_slot > interval:
                # 時計が大きく進んだ(起動直後の NTP 同期など)。古い枠では計測しない
                log.warning("clock jumped forward by %.0fs; rescheduling", now - self.next_slot)
                self.next_slot = self.schedule.next_slot(now)
            elif now >= self.next_slot:
                slot = self.next_slot
                try:
                    self.run_cycle(slot)
                except Exception:
                    log.exception("cycle failed")
                self.next_slot = self.schedule.next_slot()
                if self.next_slot - slot > 1.5 * self.schedule.current()[0]:
                    log.warning("cycle overran; skipped slot(s)")
            elif self.next_slot - now > interval + 1:
                # 周期が短く変更された / 時計が戻った場合に組み直す
                self.next_slot = self.schedule.next_slot()
        try:
            self.radio.close()
        except Exception:
            pass
        log.info("poller stopped")

    def reschedule(self) -> None:
        self.commands.put(("noop", None, queue.Queue(maxsize=1)))
