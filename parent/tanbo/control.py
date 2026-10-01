"""制御用 unix ソケット(1接続1要求、JSON 1行)。tanboctl から使う。"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


class ControlServer(threading.Thread):
    def __init__(self, path: str, app):
        super().__init__(name="control", daemon=True)
        self.path = path
        self.app = app
        self.stop_event = threading.Event()
        self._sock: socket.socket | None = None

    def run(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o660)
        s.listen(4)
        s.settimeout(1.0)
        self._sock = s
        log.info("control socket: %s", self.path)
        while not self.stop_event.is_set():
            try:
                conn, _ = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        s.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            buf = b""
            try:
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode() or "{}")
                conn.settimeout(180)
                resp = self.app.handle_control(req.get("cmd", ""), req.get("args") or {})
            except Exception as e:
                log.exception("control request failed")
                resp = {"error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode())
            except OSError:
                pass


def call(path: str, cmd: str, args: dict[str, Any] | None = None, timeout: float = 180) -> Any:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    with s:
        s.sendall((json.dumps({"cmd": cmd, "args": args or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def parse_duration(s: str) -> int:
    """'90', '30m', '2h', '1d' → 秒"""
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def fmt_ts(t: float | None) -> str:
    return "-" if t is None else time.strftime("%m-%d %H:%M:%S", time.localtime(t))
