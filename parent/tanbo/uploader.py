"""未送信行をまとめて GAS へ送るスレッド。

GAS は (db_uuid, kind) ごとに受理済みの最大 id(高水位線)を返す。
親機はその id までを送信済みにする。再送しても GAS 側で重複は弾かれる。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

MAX_BATCHES_PER_WAKE = 20
BACKOFF_MAX_S = 900


class Uploader(threading.Thread):
    def __init__(self, cfg: Config, store: Store, fw: str):
        super().__init__(name="uploader", daemon=True)
        self.cfg = cfg
        self.store = store
        self.fw = fw
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.heartbeat = time.monotonic()
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self._backoff = 0.0
        self._session = requests.Session()
        self._last_prune = 0.0

    def _post(self, kind: str, rows: list[dict[str, Any]]) -> int:
        up = self.cfg.upload
        body = {"token": up.token, "parent_id": up.parent_id, "db_uuid": self.store.db_uuid,
                "fw": self.fw, "kind": kind, "rows": rows}
        r = self._session.post(up.url, json=body, timeout=up.timeout_s)
        r.raise_for_status()
        try:
            res = r.json()
        except ValueError:
            raise RuntimeError(f"non-JSON response: {r.text[:200]!r}")
        if not res.get("ok"):
            raise RuntimeError(f"GAS error: {res.get('error')}")
        return int(res["hwm"])

    def upload_once(self) -> int:
        """送れるだけ送る。送信済みにした行数を返す。失敗時は例外。"""
        total = 0
        for kind in ("h", "m"):   # 健全性を先に(親機の生存が先に見えるように)
            for _ in range(MAX_BATCHES_PER_WAKE):
                rows = self.store.fetch_unsent(kind, self.cfg.upload.batch_max)
                if not rows:
                    break
                self.heartbeat = time.monotonic()
                hwm = self._post(kind, rows)
                if hwm < rows[0]["id"]:
                    raise RuntimeError(f"GAS hwm {hwm} < first id {rows[0]['id']}")
                total += self.store.mark_sent(kind, hwm)
                if len(rows) < self.cfg.upload.batch_max:
                    break
        return total

    def run(self) -> None:
        log.info("uploader start: %s", self.cfg.upload.url[:60])
        while not self.stop_event.is_set():
            self.heartbeat = time.monotonic()
            try:
                n = self.upload_once()
                if n:
                    log.info("uploaded %d rows", n)
                self.last_ok = time.time()
                self.last_error = None
                self._backoff = 0.0
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                self._backoff = min(BACKOFF_MAX_S, max(30.0, self._backoff * 2))
                log.warning("upload failed (retry in %.0fs): %s", self._backoff, self.last_error)

            if time.time() - self._last_prune > 86400:
                try:
                    n = self.store.prune(self.cfg.storage.retain_days)
                    if n:
                        log.info("pruned %d old rows", n)
                except Exception:
                    log.exception("prune failed")
                self._last_prune = time.time()

            until = time.monotonic() + (self._backoff or self.cfg.upload.period_s)
            while not self.stop_event.is_set() and time.monotonic() < until:
                self.heartbeat = time.monotonic()
                if self.wake.wait(timeout=5):
                    self.wake.clear()
                    self._backoff = 0.0
                    break
        log.info("uploader stopped")
