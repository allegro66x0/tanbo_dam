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
    retries: int = 1               # TIMEOUT / TX_FAIL のとき、同じサイクルの中で送り直す回数


@dataclass
class LocalCfg:
    """親機の地点のセンサ。

    type = "usb":  親機に USB でつないだ ESP32(子機と同じファーム)。port が要る
    type = "gpio": 親機の GPIO に直接つないだ HC-SR04(TRIG / ECHO。ECHO は 3.3 V に分圧)
    """
    node: int                      # この地点のノード番号([nodes] と重ならない番号)
    type: str = "usb"
    port: str = ""
    baud: int = 115200
    reply_timeout_s: float = 3.0
    chip: str = "/dev/gpiochip0"   # type = "gpio" のとき
    trig: int = 23                 # BCM 番号(物理ピン 16)
    echo: int = 24                 # BCM 番号(物理ピン 18)


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
class AssignCfg:
    """GAS の分担表を一度も受け取っていないときに測るノード(None なら [nodes] 全部)。"""
    initial: Optional[list[int]] = None


@dataclass
class Config:
    xbee: XBeeCfg
    upload: UploadCfg
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    health: HealthCfg = field(default_factory=HealthCfg)
    control: ControlCfg = field(default_factory=ControlCfg)
    web: WebCfg = field(default_factory=WebCfg)
    assign: AssignCfg = field(default_factory=AssignCfg)
    nodes: dict[int, str] = field(default_factory=dict)   # node -> MAC(大文字16桁)
    local: Optional[LocalCfg] = None

    def mac_to_node(self) -> dict[str, int]:
        return {mac: n for n, mac in self.nodes.items()}

    def node_ids(self) -> list[int]:
        """XBee の子機と親機の地点のセンサを合わせたノード番号。"""
        ids = list(self.nodes)
        if self.local is not None:
            ids.append(self.local.node)
        return sorted(ids)


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
        assign=_section(raw, "assign", AssignCfg),
    )
    if raw.get("local") is not None:
        cfg.local = _section(raw, "local", LocalCfg)
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
    if not 0 <= cfg.xbee.retries <= 5:
        raise ConfigError("xbee.retries は 0〜5")
    if cfg.local is not None:
        if cfg.local.node in cfg.nodes:
            raise ConfigError(f"local.node = {cfg.local.node} は [nodes] と重なっています")
        if cfg.local.reply_timeout_s <= 0:
            raise ConfigError("local.reply_timeout_s は正の値")
        if cfg.local.type not in ("usb", "gpio"):
            raise ConfigError("local.type は usb / gpio")
        if cfg.local.type == "usb" and not cfg.local.port:
            raise ConfigError('local.type = "usb" には port が要ります')
        if cfg.local.type == "gpio":
            if cfg.local.trig == cfg.local.echo or not all(
                    0 <= p <= 27 for p in (cfg.local.trig, cfg.local.echo)):
                raise ConfigError("local.trig / echo は別々の BCM 番号(0〜27)")
    init = cfg.assign.initial
    if init is not None:
        if not isinstance(init, list) or not all(isinstance(n, int) for n in init):
            raise ConfigError("assign.initial はノード番号の配列")
        unknown = [n for n in init if n not in cfg.nodes]
        if unknown:
            raise ConfigError(f"assign.initial のノード {unknown} が [nodes] にありません")
    if not 1 <= cfg.web.port <= 65535:
        raise ConfigError("web.port は 1〜65535")
    if cfg.health.modem not in ("none", "mmcli"):
        raise ConfigError("health.modem は none / mmcli")
    if (cfg.health.ina219_addr is None) != (cfg.health.ina219_bus is None):
        raise ConfigError("health.ina219_bus と ina219_addr は両方指定するか両方省略")
    return cfg
