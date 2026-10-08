"""親機の操作画面(Tailscale 経由でスマホから開く)。

計測サービス(tanbo-parent)とは別プロセスで動く:
- 表示は SQLite を読み取り専用で開いて作る → 計測サービスが止まっていても履歴は見られる
- 操作は制御ソケット経由で計測サービスに頼む(tanboctl と同じ口)

標準ライブラリだけで動く(Flask などは使わない)。
"""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, config as config_mod
from .control import call

log = logging.getLogger("tanbo.web")

OK_STATUSES = ("OK", "OK_V1")
ALLOWED_CMDS = {"check", "interval", "normal", "poll", "get", "flush"}
CSRF_HEADER = "X-Tanbo-Request"
HISTORY_MAX_HOURS = 24 * 31
HISTORY_MAX_ROWS = 6000


# ---------------------------------------------------------------- interface allowlist
_SIOCGIFADDR = 0x8915


def _ipv4_of(ifname: str) -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", ifname.encode()[:15])
        res = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, req)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def _ipv6_of(ifname: str) -> list[str]:
    out = []
    try:
        with open("/proc/net/if_inet6") as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 6 and p[5] == ifname:
                    out.append(str(ipaddress.IPv6Address(bytes.fromhex(p[0]))))
    except OSError:
        pass
    return out


class InterfaceAllowlist:
    """接続を受けた側(サーバ側)のアドレスが、許可 I/F のアドレスかどうかを判定する。

    LTE 側の I/F に届いた接続は拒否する。tailscale0 は起動後に現れることがあるので
    30 秒ごとにアドレスを取り直す。
    """

    def __init__(self, interfaces: list[str]):
        self.interfaces = list(interfaces)
        self._addrs: set[str] = set()
        self._at = 0.0
        self._lock = threading.Lock()

    def addresses(self) -> set[str]:
        with self._lock:
            if time.monotonic() - self._at > 30:
                a: set[str] = set()
                for ifn in self.interfaces:
                    v4 = _ipv4_of(ifn)
                    if v4:
                        a.add(v4)
                    a.update(_ipv6_of(ifn))
                if "lo" in self.interfaces:
                    a.update({"127.0.0.1", "::1"})
                self._addrs = a
                self._at = time.monotonic()
            return set(self._addrs)

    def allows(self, local_addr: str) -> bool:
        if not self.interfaces:
            return True
        if local_addr.startswith("::ffff:"):
            local_addr = local_addr[7:]
        return local_addr in self.addresses()


# ---------------------------------------------------------------- data access
class ReadOnlyDB:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def nodes(self, node_ids: list[int]) -> list[dict[str, Any]]:
        out = []
        with self._connect() as db:
            for n in node_ids:
                last = db.execute(
                    "SELECT ts, status, dist_cm, med_us, min_us, max_us, n_ok, n_try, fw, rtt_ms, "
                    "tx_status, child_uptime_s, id FROM measurements WHERE node=? "
                    "ORDER BY id DESC LIMIT 1", (n,)).fetchone()
                ok = db.execute(
                    "SELECT id, ts, dist_cm FROM measurements WHERE node=? AND status IN (?,?) "
                    "ORDER BY id DESC LIMIT 1", (n, *OK_STATUSES)).fetchone()
                fails = 0
                if last is not None:
                    fails = db.execute(
                        "SELECT COUNT(*) FROM measurements WHERE node=? AND id>?",
                        (n, ok["id"] if ok else 0)).fetchone()[0]
                row: dict[str, Any] = {"node": n, "last": None, "last_ok_ts": None,
                                       "last_ok_dist": None, "fails": fails}
                if last is not None:
                    row["last"] = {k: last[k] for k in last.keys() if k != "id"}
                if ok is not None:
                    row["last_ok_ts"] = ok["ts"]
                    row["last_ok_dist"] = ok["dist_cm"]
                out.append(row)
        return out

    def health(self, limit: int = 36) -> list[dict[str, Any]]:
        with self._connect() as db:
            cur = db.execute("SELECT * FROM health ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def history(self, node: int, hours: float) -> list[list[Any]]:
        since = time.time() - hours * 3600
        with self._connect() as db:
            cur = db.execute(
                "SELECT ts, status, dist_cm, min_us, max_us, n_ok, n_try FROM measurements "
                "WHERE node=? AND ts>=? ORDER BY ts DESC LIMIT ?", (node, since, HISTORY_MAX_ROWS))
            rows = [list(r) for r in cur.fetchall()]
        rows.reverse()
        return rows


# ---------------------------------------------------------------- HTTP
class WebApp:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self.db = ReadOnlyDB(cfg.storage.db_path)
        self.allow = InterfaceAllowlist(cfg.web.allow_interfaces)
        self.index_html = (resources.files("tanbo") / "static" / "index.html").read_bytes()

    def overview(self) -> dict[str, Any]:
        res: dict[str, Any] = {"now": time.time(), "web_version": __version__,
                               "service": None, "service_error": None,
                               "nodes": [], "health": [], "db_error": None,
                               "check_interval_s": self.cfg.schedule.check_interval_s,
                               "check_default_duration_s": self.cfg.schedule.check_default_duration_s,
                               "local_node": self.cfg.local.node if self.cfg.local else None}
        try:
            st = call(self.cfg.control.socket, "status", timeout=5)
            st.pop("latest", None)
            res["service"] = st
        except (FileNotFoundError, ConnectionRefusedError):
            res["service_error"] = "計測サービスが動いていません"
        except PermissionError:
            res["service_error"] = "制御ソケットに接続する権限がありません"
        except Exception as e:
            res["service_error"] = f"計測サービスから応答がありません({type(e).__name__})"
        try:
            res["nodes"] = self.db.nodes(self.cfg.node_ids())
            res["health"] = self.db.health()
        except sqlite3.Error as e:
            res["db_error"] = f"データベースを読めません: {e}"
            res["nodes"] = [{"node": n, "last": None, "last_ok_ts": None, "last_ok_dist": None,
                             "fails": 0} for n in self.cfg.node_ids()]
        return res

    def command(self, cmd: str, args: dict[str, Any]) -> tuple[int, Any]:
        if cmd not in ALLOWED_CMDS:
            return 400, {"error": f"使えない操作です: {cmd}"}
        clean: dict[str, Any] = {}
        try:
            if cmd in ("check", "interval") and args.get("duration") is not None:
                clean["duration"] = int(args["duration"])
            if cmd == "interval":
                clean["seconds"] = int(args["seconds"])
            if cmd == "get":
                clean["node"] = int(args["node"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "引数が正しくありません"}
        try:
            return 200, call(self.cfg.control.socket, cmd, clean, timeout=180)
        except (FileNotFoundError, ConnectionRefusedError):
            return 503, {"error": "計測サービスが動いていないので操作できません"}
        except socket.timeout:
            return 504, {"error": "計測サービスの応答が時間内に返りませんでした"}


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"tanbo-web/{__version__}"

        def log_message(self, fmt, *a):
            log.debug("%s %s", self.address_string(), fmt % a)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")

        def _allowed(self) -> bool:
            local = self.connection.getsockname()[0]
            if app.allow.allows(local):
                return True
            log.warning("rejected %s on %s (not an allowed interface)", self.client_address[0], local)
            self._send(403, b"forbidden", "text/plain")
            return False

        def do_GET(self):
            if not self._allowed():
                return
            u = urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    self._send(200, app.index_html, "text/html; charset=utf-8")
                elif u.path == "/api/overview":
                    self._json(200, app.overview())
                elif u.path == "/api/history":
                    q = parse_qs(u.query)
                    node = int(q["node"][0])
                    hours = min(float(q.get("hours", ["24"])[0]), HISTORY_MAX_HOURS)
                    if node not in app.cfg.node_ids():
                        self._json(404, {"error": "そのノードは設定にありません"})
                        return
                    self._json(200, {"node": node, "hours": hours,
                                     "cols": ["ts", "status", "dist_cm", "min_us", "max_us",
                                              "n_ok", "n_try"],
                                     "rows": app.db.history(node, hours)})
                else:
                    self._send(404, b"not found", "text/plain")
            except (KeyError, ValueError):
                self._json(400, {"error": "パラメータが正しくありません"})
            except sqlite3.Error as e:
                self._json(500, {"error": f"データベースを読めません: {e}"})

        def do_POST(self):
            if not self._allowed():
                return
            if urlparse(self.path).path != "/api/cmd":
                self._send(404, b"not found", "text/plain")
                return
            # 独自ヘッダ必須 → 他サイトのページからのフォーム送信・単純な fetch は通らない
            if self.headers.get(CSRF_HEADER) != "1":
                self._json(403, {"error": "missing header"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4096:
                    raise ValueError("too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                cmd = str(body.get("cmd", ""))
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    raise ValueError("args")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "リクエストが正しくありません"})
                return
            log.info("command %s %s from %s", cmd, args, self.client_address[0])
            code, res = app.command(cmd, args)
            if code == 200 and isinstance(res, dict) and res.get("error"):
                code = 400
            self._json(code, res)

    return Handler


def serve(cfg: config_mod.Config, host: Optional[str] = None,
          port: Optional[int] = None) -> ThreadingHTTPServer:
    app = WebApp(cfg)
    srv = ThreadingHTTPServer((host or cfg.web.bind, cfg.web.port if port is None else port),
                              make_handler(app))
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="田んぼダム 親機の操作画面")
    ap.add_argument("-c", "--config", default="/etc/tanbo/parent.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if os.environ.get("INVOCATION_ID")
        else "%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(a.config)
    srv = serve(cfg)
    log.info("tanbo-web %s on %s:%d (interfaces: %s)", __version__, cfg.web.bind, cfg.web.port,
             ",".join(cfg.web.allow_interfaces) or "all")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
