"""GAS の分担表(どの親機がどのノードを測るか)。

- 送信の応答に毎回入ってくる {"<node>": "<parent_id>"} を受け取り、変わったら SQLite(meta)に保存する。
  回線が切れても、再起動しても、最後に受け取った表で測り続ける
- 分担表を一度も受け取っていなければ、設定 [assign] initial のノード(書いていなければ [nodes] 全部)を測る
- 親機の地点のセンサ([local])は表に関係なく自分で測る(ほかの親機は測れないため)
- GAS から受け取るのは分担表だけ。それ以外の命令は受け付けない
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

META_KEY = "assign"
SAVE_EVERY_S = 3600


class Assignment:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._lock = threading.Lock()
        self.table: Optional[dict[int, str]] = None   # node -> parent_id
        self.received_ts: Optional[float] = None
        self._warned_unknown: set[int] = set()
        self._saved_ts = 0.0
        raw = store.get_meta(META_KEY)
        if raw:
            try:
                saved = json.loads(raw)
                self.table = {int(k): str(v) for k, v in saved["table"].items()}
                self.received_ts = float(saved["ts"])
            except (ValueError, KeyError, TypeError) as e:
                log.warning("保存されていた分担表が読めないので使わない: %s", e)

    @staticmethod
    def _parse(raw: Any) -> Optional[dict[int, str]]:
        if not isinstance(raw, dict):
            return None
        out: dict[int, str] = {}
        for k, v in raw.items():
            try:
                node = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str) and v:
                out[node] = v
        return out

    def update(self, raw: Any) -> bool:
        """GAS の応答の assign を取り込む。形が正しくなければ無視する。変わったら True。"""
        table = self._parse(raw)
        if table is None:
            return False
        now = time.time()
        with self._lock:
            changed = table != self.table
            self.table = table
            self.received_ts = now
        if changed:
            mine = self.my_nodes()
            log.info("分担表を受け取った: このノードを測る %s(表全体 %s)", mine,
                     {n: p for n, p in sorted(table.items())})
        # SD カードへの書き込みを減らすため、保存は変わったときと 1 時間ごとだけ
        if changed or now - self._saved_ts > SAVE_EVERY_S:
            self.store.set_meta(META_KEY, json.dumps({"ts": now, "table": {str(k): v for k, v in table.items()}}))
            self._saved_ts = now
        return changed

    def my_nodes(self) -> list[int]:
        """この親機が XBee で測るノード(親機の地点のノードは含まない)。"""
        me = self.cfg.upload.parent_id
        with self._lock:
            table = self.table
        if table is None:
            init = self.cfg.assign.initial
            return sorted(self.cfg.nodes) if init is None else sorted(init)
        out = []
        for node, pid in sorted(table.items()):
            if pid != me:
                continue
            if node in self.cfg.nodes:
                out.append(node)
            elif not (self.cfg.local and node == self.cfg.local.node) and node not in self._warned_unknown:
                log.warning("分担表で node %d が %s の担当だが、[nodes] にアドレスがないので測れない", node, me)
                self._warned_unknown.add(node)
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            source = "gas" if self.table is not None else "initial"
            ts = self.received_ts
        return {"source": source, "received_ts": ts, "nodes": self.my_nodes()}
