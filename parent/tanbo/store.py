"""ローカル SQLite ストア(store-and-forward のバッファ兼アーカイブ)。

- すべての計測は先にここへ書き、送信済みフラグで管理する
- db_uuid は DB 作成時に1回だけ生成。GAS 側の重複排除キーに使う
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

MEAS_COLS = [
    "cycle_ts", "ts", "synced", "boot_id", "node", "mac", "status", "seq",
    "n_ok", "n_try", "med_us", "min_us", "max_us", "dist_cm", "child_uptime_s",
    "fw", "tx_status", "rtt_ms", "raw",
]
HEALTH_COLS = [
    "ts", "synced", "boot_id", "uptime_s", "interval_s", "cycle_ms", "n_ok", "n_nodes",
    "cpu_temp", "load1", "mem_avail_mb", "disk_free_mb", "queue_m", "queue_h",
    "batt_v", "modem_sig", "xbee_ai", "upload_err", "fw",
]
TABLES = {"m": ("measurements", MEAS_COLS), "h": ("health", HEALTH_COLS)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts REAL NOT NULL, ts REAL NOT NULL, synced INTEGER, boot_id TEXT,
    node INTEGER NOT NULL, mac TEXT NOT NULL, status TEXT NOT NULL, seq INTEGER,
    n_ok INTEGER, n_try INTEGER, med_us INTEGER, min_us INTEGER, max_us INTEGER,
    dist_cm REAL, child_uptime_s INTEGER, fw TEXT, tx_status INTEGER, rtt_ms INTEGER,
    raw TEXT, sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_meas_unsent ON measurements(sent, id);
CREATE INDEX IF NOT EXISTS ix_meas_node_id ON measurements(node, id);
CREATE INDEX IF NOT EXISTS ix_meas_node_ts ON measurements(node, ts);
CREATE TABLE IF NOT EXISTS health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, synced INTEGER, boot_id TEXT, uptime_s REAL, interval_s INTEGER,
    cycle_ms INTEGER, n_ok INTEGER, n_nodes INTEGER, cpu_temp REAL, load1 REAL,
    mem_avail_mb REAL, disk_free_mb REAL, queue_m INTEGER, queue_h INTEGER,
    batt_v REAL, modem_sig INTEGER, xbee_ai INTEGER, upload_err TEXT, fw TEXT,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_health_unsent ON health(sent, id);
"""


class Store:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                                   timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key='db_uuid'").fetchone()
            if row is None:
                self.db_uuid = uuid.uuid4().hex
                self._db.execute("INSERT INTO meta VALUES ('db_uuid', ?)", (self.db_uuid,))
                self._db.execute("INSERT INTO meta VALUES ('created', ?)", (str(time.time()),))
            else:
                self.db_uuid = row[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _insert(self, table: str, cols: list[str], rows: Iterable[dict[str, Any]]) -> int:
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        data = [tuple(r.get(c) for c in cols) for r in rows]
        if not data:
            return 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, data)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return len(data)

    def add_measurements(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("measurements", MEAS_COLS, rows)

    def add_health(self, row: dict[str, Any]) -> int:
        return self._insert("health", HEALTH_COLS, [row])

    def fetch_unsent(self, kind: str, limit: int) -> list[dict[str, Any]]:
        table, cols = TABLES[kind]
        with self._lock:
            cur = self._db.execute(
                f"SELECT id,{','.join(cols)} FROM {table} WHERE sent=0 ORDER BY id LIMIT ?",
                (limit,))
            return [dict(r) for r in cur.fetchall()]

    def mark_sent(self, kind: str, upto_id: int) -> int:
        table, _ = TABLES[kind]
        with self._lock:
            cur = self._db.execute(f"UPDATE {table} SET sent=1 WHERE sent=0 AND id<=?",
                                   (upto_id,))
            return cur.rowcount

    def unsent_count(self, kind: str) -> int:
        table, _ = TABLES[kind]
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM {table} WHERE sent=0").fetchone()[0]

    def latest_by_node(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT m.* FROM measurements m JOIN "
                "(SELECT node, MAX(id) AS mid FROM measurements GROUP BY node) x "
                "ON m.id = x.mid ORDER BY m.node")
            return [dict(r) for r in cur.fetchall()]

    def prune(self, retain_days: int) -> int:
        cutoff = time.time() - retain_days * 86400
        n = 0
        with self._lock:
            for table, _ in TABLES.values():
                n += self._db.execute(f"DELETE FROM {table} WHERE sent=1 AND ts<?",
                                      (cutoff,)).rowcount
        return n
