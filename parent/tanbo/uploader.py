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


def _root_cause(e: BaseException) -> BaseException:
    """requests / urllib3 が包んだ例外をたどって、元の原因を返す。"""
    seen = set()
    while id(e) not in seen:
        seen.add(id(e))
        nxt = getattr(e, "reason", None) or e.__cause__ or e.__context__
        if not isinstance(nxt, BaseException) and e.args and isinstance(e.args[0], BaseException):
            nxt = e.args[0]
        if not isinstance(nxt, BaseException):
            break
        e = nxt
    return e


def describe_error(e: BaseException) -> str:
    """送信失敗を「分類: 原因」の短い1行にする(journal・tanboctl・health.upload_err 共通)。

      NET      回線・DNS・接続先(相手に届いていない)
      TIMEOUT  接続または応答の時間切れ
      HTTP     HTTP ステータスが 4xx / 5xx
      RESP     JSON でない応答(公開範囲が「全員」でないとログイン画面が返る)
      GAS      GAS が ok:false を返した(bad token など)
      ERR      その他
    """
    if isinstance(e, UploadRejected):
        return f"{e.kind}: {e}"[:200]
    if isinstance(e, requests.exceptions.Timeout):
        what = "connect" if isinstance(e, requests.exceptions.ConnectTimeout) else "read"
        return f"TIMEOUT: {what}"
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"HTTP: {e.response.status_code} {e.response.reason or ''}".strip()
    if isinstance(e, requests.exceptions.ConnectionError):
        root = _root_cause(e)
        msg = str(root)
        # "HTTPConnection(host=..., port=..): Failed to establish a new connection: [Errno 111] ..."
        # のような前置きを落として末尾の原因だけ残す
        for sep in ("Failed to establish a new connection: ", "Failed to resolve "):
            if sep in msg:
                msg = ("resolve " if "resolve" in sep else "") + msg.split(sep, 1)[1]
                break
        return f"NET: {type(root).__name__}: {msg[-150:]}"
    return f"ERR: {type(e).__name__}: {str(e)[-150:]}"


class UploadRejected(RuntimeError):
    """相手には届いたが受理されなかった。kind は describe_error の分類。"""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


class Uploader(threading.Thread):
    def __init__(self, cfg: Config, store: Store, fw: str):
        super().__init__(name="uploader", daemon=True)
        self.cfg = cfg
        self.store = store
        self.fw = fw
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.heartbeat = time.monotonic()
        self.last_ok: Optional[float] = None       # 最後に POST が受理された時刻
        self.last_error: Optional[str] = None
        self.error_since: Optional[float] = None   # 連続失敗が始まった時刻
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
            raise UploadRejected("RESP", f"non-JSON response: {r.text[:80]!r}")
        if not res.get("ok"):
            raise UploadRejected("GAS", str(res.get("error")))
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
                self.last_ok = time.time()
                if hwm < rows[0]["id"]:
                    raise UploadRejected("GAS", f"hwm {hwm} < first id {rows[0]['id']}")
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
                if self.last_error:
                    log.info("upload recovered")
                self.last_error = None
                self.error_since = None
                self._backoff = 0.0
            except Exception as e:
                self.last_error = describe_error(e)
                if self.error_since is None:
                    self.error_since = time.time()
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
