"""底层网络状态：网卡、DNS、网络位置与默认路由出口（用于网络适用性判断）。"""
from __future__ import annotations

from pathlib import Path

from .base import run_cmd, run_ps


def read_hosts() -> dict[str, str]:
    """解析 hosts 中的有效映射（忽略注释）。"""
    entries: dict[str, str] = {}
    path = Path(r"C:\Windows\System32\drivers\etc\hosts")
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            entries[parts[1].lower()] = parts[0]
    return entries


def connection_identity() -> dict:
    """当前默认路由出口网络的标识：{ssid, profile, interface}。多网卡时取默认路由。"""
    out: dict = {"ssid": None, "profile": None, "interface": None}
    raw = run_ps(
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
        "Sort-Object RouteMetric | Select-Object -First 1; "
        "if ($r) { "
        "  $p = Get-NetConnectionProfile -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue; "
        "  '{0}|{1}|{2}' -f $r.InterfaceIndex, $p.Name, $p.InterfaceAlias "
        "}",
        timeout=20,
    ).strip()
    if raw and "|" in raw:
        if_index, profile, alias = raw.split("|", 2)
        out["profile"] = profile or None
        out["interface"] = alias or None
        wlan = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=20)
        current = False
        for line in wlan.splitlines():
            if "SSID" in line and "BSSID" not in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    out["ssid"] = parts[1].strip() or None
                    current = True
            elif current is False and " State" in line:
                pass
        # netsh 输出的是本机所有 WLAN 接口；单 WLAN 网卡场景足够。
        if not out.get("ssid"):
            out["ssid"] = None
    return out
