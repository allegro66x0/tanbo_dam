"""親機自身の状態取得(時刻同期、温度、資源、任意のバッテリー電圧と電波強度)。

どれも失敗したら None を返し、本体の動作は止めない。
"""
from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

_TIME_ERROR = 5


def boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def clock_synced() -> Optional[bool]:
    """カーネルの NTP 同期状態(timesyncd / chrony どちらでも有効)。"""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(512)   # struct timex(modes=0 で読み取りのみ)
        state = libc.adjtimex(buf)
        if state < 0:
            return None
        return state != _TIME_ERROR
    except Exception:
        return None


def uptime_s() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return None


def cpu_temp() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def load1() -> Optional[float]:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def mem_avail_mb() -> Optional[float]:
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return round(int(ln.split()[1]) / 1024.0, 1)
    except (OSError, ValueError):
        pass
    return None


def disk_free_mb(path: str) -> Optional[float]:
    try:
        return round(shutil.disk_usage(path).free / 1048576.0, 1)
    except OSError:
        return None


_I2C_SLAVE = 0x0703


def ina219_bus_voltage(bus: int, addr: int) -> Optional[float]:
    """INA219 のバス電圧(V)。レジスタ 0x02、LSB 4mV。シャント設定は不要。"""
    try:
        fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, addr)
        os.write(fd, bytes([0x02]))
        hi, lo = os.read(fd, 2)
        raw = (hi << 8) | lo
        if raw & 0x01:   # OVF
            return None
        return round(((raw >> 3) * 4) / 1000.0, 3)
    except OSError:
        return None
    finally:
        os.close(fd)


def modem_signal_mmcli(timeout: float = 5.0) -> Optional[int]:
    """ModemManager の電波品質(%)。"""
    try:
        out = subprocess.run(["mmcli", "-m", "any", "--output-keyvalue"],
                             capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        if k.strip() == "modem.generic.signal-quality.value":
            try:
                return int(v.strip())
            except ValueError:
                return None
    return None


def now_ts() -> tuple[float, Optional[bool]]:
    return time.time(), clock_synced()
