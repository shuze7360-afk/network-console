"""进程与端口采集：相关进程、指定端口的监听归属。"""
from __future__ import annotations

from .base import ps_json


def _as_list(data) -> list:
    if isinstance(data, dict):
        return [data]
    return data or []


def interesting_processes(pattern: str = "proxy|service|server") -> list[dict]:
    """按可配置名称模式查询进程（公开版不做任何内置猜测）。"""
    try:
        rows = ps_json(
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -match '{pattern}' }} | "
            "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate | "
            "ConvertTo-Json -Depth 2"
        )
        return rows if isinstance(rows, list) else ([rows] if rows else [])
    except Exception:
        return []


def listening_ports(ports: list[int]) -> list[dict]:
    """查询指定端口的监听行（netstat 能看到提权进程的 0.0.0.0 监听）。"""
    from .base import run_cmd

    wanted = {int(p) for p in ports}
    out = run_cmd(["netstat", "-ano", "-p", "tcp"], timeout=20)
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            addr, _, port = parts[1].rpartition(":")
            if port.isdigit() and int(port) in wanted:
                rows.append({"LocalAddress": addr, "LocalPort": int(port),
                             "OwningProcess": int(parts[4])})
    return rows
