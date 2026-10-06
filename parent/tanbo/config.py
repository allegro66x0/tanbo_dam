"""設定ファイル(TOML)の読み込みと検証。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ConfigError(ValueError):
    pass


@dataclass
class XBeeCfg:
    port: str
    baud: int = 9600
    reply_timeout_s: float = 3.0   # REQ 送信から応答を待つ上限
    gap_s: float = 0.2             # ノード間の送信間隔


@dataclass
class ScheduleCfg:
    interval_s: int = 600                  # 通常運用の周期
    check_interval_s: int = 60             # 設置チェック時の周期
    check_default_duration_s: int = 7200   # チェックモードの既定継続時間
    check_max_duration_s: int = 86400
    offset_s: int = 0                      # 壁時計の境界からのずらし


@dataclass
class UploadCfg:
    url: str
    token: str
    parent_id: str = "parent-1"
    batch_max: int = 500
    period_s: int = 60
    timeout_s: int = 40


@dataclass
class StorageCfg:
    db_path: str = "/var/lib/tanbo/tanbo.db"
    retain_days: int = 365


@dataclass
class HealthCfg:
    ina219_bus: Optional[int] = None
    ina219_addr: Optional[int] = None
    modem: str = "none"   # "none" | "mmcli"


@dataclass
class ControlCfg:
    socket: str = "/run/tanbo/ctl.sock"


@dataclass
class WebCfg:
    bind: str = "0.0.0.0"
    port: int = 8080
    # この I/F に届いた接続だけ受け付ける(LTE 側からは開けない)。空リストなら制限なし
    allow_interfaces: list[str] = field(default_factory=lambda: ["lo", "tailscale0"])


@dataclass
class Config:
    xbee: XBeeCfg
    upload: UploadCfg
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    health: HealthCfg = field(default_factory=HealthCfg)
    control: ControlCfg = field(default_factory=ControlCfg)
    web: WebCfg = field(default_factory=WebCfg)
    nodes: dict[int, str] = field(default_factory=dict)   # node -> MAC(大文字16桁)

    def mac_to_node(self) -> dict[str, int]:
        return {mac: n for n, mac in self.nodes.items()}


def _section(raw: dict, name: str, cls, required: bool = False):
    sec = raw.get(name)
    if sec is None:
        if required:
            raise ConfigError(f"[{name}] がありません")
        return cls()
    try:
        return cls(**sec)
    except TypeError as e:
        raise ConfigError(f"[{name}]: {e}") from e


def _norm_mac(mac: str) -> str:
    m = mac.replace(":", "").replace("-", "").strip().upper()
    if len(m) != 16 or any(c not in "0123456789ABCDEF" for c in m):
        raise ConfigError(f"MAC アドレスの形式が不正: {mac!r}")
    return m


def load(path: str | Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config(
        xbee=_section(raw, "xbee", XBeeCfg, required=True),
        upload=_section(raw, "upload", UploadCfg, required=True),
        schedule=_section(raw, "schedule", ScheduleCfg),
        storage=_section(raw, "storage", StorageCfg),
        health=_section(raw, "health", HealthCfg),
        control=_section(raw, "control", ControlCfg),
        web=_section(raw, "web", WebCfg),
    )
    nodes = {}
    for k, v in (raw.get("nodes") or {}).items():
        try:
            n = int(k)
        except ValueError as e:
            raise ConfigError(f"[nodes] のキーは整数: {k!r}") from e
        nodes[n] = _norm_mac(v)
    if len(set(nodes.values())) != len(nodes):
        raise ConfigError("[nodes] に重複した MAC があります")
    cfg.nodes = dict(sorted(nodes.items()))

    s = cfg.schedule
    for name in ("interval_s", "check_interval_s"):
        if not 10 <= getattr(s, name) <= 86400:
            raise ConfigError(f"schedule.{name} は 10〜86400 秒")
    if cfg.xbee.reply_timeout_s <= 0:
        raise ConfigError("xbee.reply_timeout_s は正の値")
    if not 1 <= cfg.web.port <= 65535:
        raise ConfigError("web.port は 1〜65535")
    if cfg.health.modem not in ("none", "mmcli"):
        raise ConfigError("health.modem は none / mmcli")
    if (cfg.health.ina219_addr is None) != (cfg.health.ina219_bus is None):
        raise ConfigError("health.ina219_bus と ina219_addr は両方指定するか両方省略")
    return cfg
