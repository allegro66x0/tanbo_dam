"""子機 ↔ 親機 テキストプロトコル(docs/PROTOCOL.md)の生成と解析。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SOUND_CM_PER_US_20C = 0.034340  # 343.4 m/s
MISSING = -999.0


def build_req(seq: int) -> bytes:
    return f"REQ,{seq & 0xFFFF}\n".encode("ascii")


def us_to_cm_20c(us: Optional[int]) -> Optional[float]:
    if us is None or us < 0:
        return None
    return round(us * SOUND_CM_PER_US_20C / 2.0, 2)


@dataclass
class Reply:
    version: int                 # 1 or 2
    seq: Optional[int] = None
    n_ok: Optional[int] = None
    n_try: Optional[int] = None
    med_us: Optional[int] = None
    min_us: Optional[int] = None
    max_us: Optional[int] = None
    child_uptime_s: Optional[int] = None
    fw: Optional[str] = None
    dist_cm: Optional[float] = None   # v1: 子機が計算した距離 / v2: 20℃名目値


class ParseError(ValueError):
    pass


def _int_or_none(s: str) -> Optional[int]:
    v = int(s)
    return None if v < 0 else v


def parse_reply(raw: bytes) -> Reply:
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as e:
        raise ParseError(f"non-ascii: {raw!r}") from e
    if not text:
        raise ParseError("empty")
    # 1フレームに複数行が入ることは想定しないが、最後の非空行を採用する
    text = [ln for ln in text.splitlines() if ln.strip()][-1].strip()
    parts = [p.strip() for p in text.split(",")]

    if parts[0] == "D2":
        if len(parts) != 9:
            raise ParseError(f"D2 field count {len(parts)}: {text!r}")
        try:
            seq = int(parts[1])
            n_ok, n_try = int(parts[2]), int(parts[3])
            med, mn, mx = (_int_or_none(p) for p in parts[4:7])
            up = int(parts[7])
        except ValueError as e:
            raise ParseError(f"D2 bad number: {text!r}") from e
        if not (0 <= n_ok <= n_try <= 255):
            raise ParseError(f"D2 bad counts: {text!r}")
        if n_ok > 0 and med is None:
            raise ParseError(f"D2 n_ok>0 but no median: {text!r}")
        return Reply(
            version=2, seq=None if seq < 0 else seq, n_ok=n_ok, n_try=n_try,
            med_us=med if n_ok else None, min_us=mn if n_ok else None,
            max_us=mx if n_ok else None, child_uptime_s=up, fw=parts[8] or None,
            dist_cm=us_to_cm_20c(med) if n_ok else None,
        )

    if parts[0].startswith("ID"):
        if len(parts) == 2 and parts[1] == "NoData":
            return Reply(version=1, dist_cm=None)
        if len(parts) != 5:
            raise ParseError(f"v1 field count {len(parts)}: {text!r}")
        try:
            d = float(parts[1])
        except ValueError as e:
            raise ParseError(f"v1 bad number: {text!r}") from e
        return Reply(version=1, dist_cm=None if d <= MISSING + 0.5 else d)

    raise ParseError(f"unknown reply: {text!r}")
