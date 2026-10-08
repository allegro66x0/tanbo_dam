"""tanboctl — 親機サービスの操作 CLI。

  tanboctl status            状態と各ノードの最新値
  tanboctl check [2h]        設置チェック用の短周期(既定 60 秒)に一時切り替え
  tanboctl interval 120 [30m]  任意の周期に一時切り替え
  tanboctl normal            通常周期に戻す
  tanboctl get 5             ノード 5 に今すぐ1回問い合わせ(記録しない)
  tanboctl poll              全ノードを今すぐ1サイクル計測(記録する)
  tanboctl flush             未送信データを今すぐ送る
"""
from __future__ import annotations

import argparse
import json
import sys

from .control import call, fmt_ts, parse_duration

DEFAULT_SOCK = "/run/tanbo/ctl.sock"


def _print_status(s: dict) -> None:
    ov = f"(一時変更 〜{fmt_ts(s['override_until'])})" if s.get("override_until") else "(通常)"
    print(f"親機 v{s['version']}  周期 {s['interval_s']}s {ov}  次回 {fmt_ts(s['next_slot'])}")
    print(f"時刻同期 {s['clock_synced']}  XBee {'OK' if s['radio_open'] else 'NG'} "
          f"{s.get('local_mac') or ''}")
    if s.get("local_node") is not None:
        print(f"親機の地点のセンサ ノード {s['local_node']}  {'OK' if s['local_open'] else 'NG'}"
              f"{'  ' + s['local_error'] if s.get('local_error') else ''}")
    lc = s.get("last_cycle") or {}
    if lc:
        print(f"前回サイクル {fmt_ts(lc['cycle_ts'])}  {lc['n_ok']}/{lc['n_nodes']} OK  "
              f"{lc['cycle_ms']} ms")
    up = s["unsent"]
    print(f"未送信 計測 {up['m']} / 健全性 {up['h']}  最終送信成功 {fmt_ts(s['upload_last_ok'])}")
    if s.get("upload_error"):
        print(f"送信エラー({fmt_ts(s.get('upload_error_since'))}〜): {s['upload_error']}")
    print()
    print(f"{'node':>4}  {'時刻':<14} {'status':<9} {'tx':>4} {'rtt[ms]':>7} {'dist20[cm]':>10} "
          f"{'med[us]':>8} {'ok/try':>6}  fw")
    for r in s.get("latest", []):
        d = "-" if r["dist_cm"] is None else f"{r['dist_cm']:.1f}"
        m = "-" if r["med_us"] is None else str(r["med_us"])
        ot = "-" if r["n_try"] is None else f"{r['n_ok']}/{r['n_try']}"
        tx = "-" if r.get("tx_status") is None else f"0x{r['tx_status']:02X}"
        rtt = "-" if r.get("rtt_ms") is None else str(r["rtt_ms"])
        print(f"{r['node']:>4}  {fmt_ts(r['ts']):<14} {r['status']:<9} {tx:>4} {rtt:>7} {d:>10} "
              f"{m:>8} {ot:>6}  {r['fw'] or '-'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tanboctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=DEFAULT_SOCK)
    ap.add_argument("--json", action="store_true", help="結果を JSON のまま出す")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("check"); p.add_argument("duration", nargs="?")
    p = sub.add_parser("interval"); p.add_argument("seconds"); p.add_argument("duration", nargs="?")
    sub.add_parser("normal")
    p = sub.add_parser("get"); p.add_argument("node", type=int)
    sub.add_parser("poll")
    sub.add_parser("flush")
    a = ap.parse_args(argv)

    args: dict = {}
    if a.cmd in ("check", "interval") and a.duration:
        args["duration"] = parse_duration(a.duration)
    if a.cmd == "interval":
        args["seconds"] = parse_duration(a.seconds)
    if a.cmd == "get":
        args["node"] = a.node

    try:
        res = call(a.socket, a.cmd, args)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"サービスに接続できません ({a.socket})。systemctl status tanbo-parent を確認",
              file=sys.stderr)
        return 1
    except PermissionError:
        print(f"{a.socket} へのアクセス権がありません(sudo か tanbo グループで実行)",
              file=sys.stderr)
        return 1

    if isinstance(res, dict) and res.get("error"):
        print("エラー:", res["error"], file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif a.cmd == "status":
        _print_status(res)
    elif a.cmd in ("check", "interval"):
        print(f"周期 {res['interval_s']}s に変更(〜{fmt_ts(res['until'])}、その後は通常周期)")
    elif a.cmd == "normal":
        print(f"通常周期 {res['interval_s']}s に戻しました")
    elif a.cmd == "poll":
        for n, st in res.items():
            print(f"node {n:>2}: {st}")
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
