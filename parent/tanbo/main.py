"""田んぼダム 親機サービスのエントリポイント。"""
from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import signal
import socket
import sys
import time
from typing import Any

from . import __version__, config as config_mod, sysinfo
from .control import ControlServer
from .local import LocalSensor
from .poller import Poller
from .radio import XBeeRadio
from .store import Store
from .uploader import Uploader

log = logging.getLogger("tanbo")

POLLER_STALL_S = 180
UPLOADER_STALL_S = 300


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


class JournalFormatter(logging.Formatter):
    """systemd 配下では行頭に <優先度> を付ける(journalctl -p warning で絞れるようにする)。"""
    PRIO = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3,
            logging.CRITICAL: 2}

    def format(self, record: logging.LogRecord) -> str:
        prio = self.PRIO.get(record.levelno, 6)
        # 複数行(トレースバック)は各行に付けないと 2 行目以降が info になる
        return "\n".join(f"<{prio}>{line}" for line in super().format(record).splitlines())


class App:
    def __init__(self, cfg: config_mod.Config, radio=None):
        self.cfg = cfg
        self.store = Store(cfg.storage.db_path)
        self.radio = radio or XBeeRadio(cfg.xbee.port, cfg.xbee.baud)
        self.local = LocalSensor(cfg.local.port, cfg.local.baud) if cfg.local else None
        self.uploader = Uploader(cfg, self.store, __version__)
        self.poller = Poller(cfg, self.radio, self.store, self._health_extra, self.local)
        self.control = ControlServer(cfg.control.socket, self)
        self.stopping = False

    def _health_extra(self) -> dict[str, Any]:
        h: dict[str, Any] = {
            "queue_m": self.store.unsent_count("m"),
            "queue_h": self.store.unsent_count("h"),
            "disk_free_mb": sysinfo.disk_free_mb(os.path.dirname(self.cfg.storage.db_path) or "."),
            "upload_err": self.uploader.last_error,
            "fw": __version__,
        }
        hc = self.cfg.health
        if hc.ina219_addr is not None:
            h["batt_v"] = sysinfo.ina219_bus_voltage(hc.ina219_bus, hc.ina219_addr)
        if hc.modem == "mmcli":
            h["modem_sig"] = sysinfo.modem_signal_mmcli()
        return h

    # ---- control commands ---------------------------------------------------
    def handle_control(self, cmd: str, args: dict[str, Any]) -> Any:
        sched = self.poller.schedule
        sc = self.cfg.schedule
        if cmd == "status":
            interval, until = sched.current()
            return {
                "version": __version__,
                "interval_s": interval, "base_interval_s": sched.base_interval,
                "override_until": until, "next_slot": self.poller.next_slot,
                "clock_synced": sysinfo.clock_synced(),
                "radio_open": self.radio.is_open, "local_mac": getattr(self.radio, "local_mac", None),
                # 親機の地点のセンサ([local] がなければ None)
                "local_node": self.cfg.local.node if self.cfg.local else None,
                "local_open": self.local.is_open if self.local else None,
                "local_error": self.poller.local_error,
                "last_cycle": self.poller.last_cycle,
                "latest": [{k: r[k] for k in ("node", "ts", "status", "dist_cm", "med_us",
                                               "n_ok", "n_try", "fw", "tx_status", "rtt_ms")}
                           for r in self.store.latest_by_node()],
                "unsent": {"m": self.store.unsent_count("m"), "h": self.store.unsent_count("h")},
                "upload_last_ok": self.uploader.last_ok, "upload_error": self.uploader.last_error,
                "upload_error_since": self.uploader.error_since,
            }
        if cmd in ("check", "interval"):
            seconds = int(args.get("seconds") or sc.check_interval_s)
            duration = int(args.get("duration") or sc.check_default_duration_s)
            if not 10 <= seconds <= 86400:
                return {"error": "周期は 10〜86400 秒"}
            if not 0 < duration <= sc.check_max_duration_s:
                return {"error": f"継続時間は {sc.check_max_duration_s} 秒以内"}
            until = sched.set_override(seconds, duration)
            self.poller.reschedule()
            log.info("interval override %ds until %s", seconds, time.ctime(until))
            return {"interval_s": seconds, "until": until}
        if cmd == "normal":
            sched.clear_override()
            self.poller.reschedule()
            log.info("interval override cleared")
            return {"interval_s": sched.base_interval}
        if cmd == "get":
            return self.poller.submit("get", args.get("node"))
        if cmd == "poll":
            res = self.poller.submit("poll")
            self.uploader.wake.set()
            return res
        if cmd == "flush":
            self.uploader.wake.set()
            return {"ok": True}
        return {"error": f"unknown command: {cmd}"}

    # ---- lifecycle ------------------------------------------------------------
    def run(self) -> int:
        self.uploader.start()
        self.poller.start()
        self.control.start()
        sd_notify("READY=1")
        log.info("tanbo-parent %s started (%d nodes)", __version__, len(self.cfg.node_ids()))

        rc = 0
        while not self.stopping:
            time.sleep(5)
            now = time.monotonic()
            if self.poller.fatal:
                log.critical("fatal: %s", self.poller.fatal)
                rc = 1
                break
            if not self.poller.is_alive():
                log.critical("poller thread died")
                rc = 1
                break
            stalled = []
            if now - self.poller.heartbeat > POLLER_STALL_S:
                stalled.append("poller")
            if now - self.uploader.heartbeat > UPLOADER_STALL_S or not self.uploader.is_alive():
                stalled.append("uploader")
            if stalled:
                # watchdog を止める → WatchdogSec 経過で systemd が再起動
                log.error("stalled: %s (watchdog withheld)", ",".join(stalled))
                continue
            sd_notify("WATCHDOG=1")
        self.shutdown()
        return rc

    def shutdown(self) -> None:
        sd_notify("STOPPING=1")
        for t in (self.poller, self.uploader, self.control):
            t.stop_event.set()
        self.uploader.wake.set()
        self.poller.join(timeout=30)
        self.uploader.join(timeout=60)
        self.control.join(timeout=5)
        self.store.close()
        log.info("stopped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="田んぼダム 親機サービス")
    ap.add_argument("-c", "--config", default="/etc/tanbo/parent.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("INVOCATION_ID"):
        handler.setFormatter(JournalFormatter("%(levelname)s %(threadName)s %(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, handlers=[handler])
    logging.getLogger("digi.xbee").setLevel(logging.WARNING)
    faulthandler.enable()   # ウォッチドッグの SIGABRT 時に全スレッドのスタックを journal へ
    cfg = config_mod.load(a.config)
    if not cfg.node_ids():
        log.error("[nodes] も [local] もありません")
        return 2
    app = App(cfg)

    def _stop(signum, frame):
        log.info("signal %d", signum)
        app.stopping = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
