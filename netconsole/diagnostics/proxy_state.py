"""系统代理状态：WinINET 注册表、WinHTTP、各作用域环境变量（只读采集）。"""
from __future__ import annotations

import os

from .base import run_cmd, run_ps

INET_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
ENV_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]


def read_wininet() -> dict:
    out = run_cmd(["reg", "query", INET_KEY])
    values = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": "", "AutoConfigURL": ""}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] in values:
            raw = parts[2].strip()
            if parts[1] == "REG_DWORD":
                try:
                    values[parts[0]] = int(raw, 16)
                except ValueError:
                    pass
            else:
                values[parts[0]] = raw
    return values


def write_wininet(name: str, vtype: str, value: str) -> None:
    run_cmd(["reg", "add", INET_KEY, "/v", name, "/t", vtype, "/d", value, "/f"])


def env_vars() -> dict:
    out = run_ps(
        "[pscustomobject]@{"
        + ";".join(f"{scope}_{n}=[string][Environment]::GetEnvironmentVariable('{n}','{scope}')"
                   for scope in ("User", "Machine") for n in ENV_NAMES)
        + "} | ConvertTo-Json"
    )
    import json

    try:
        flat = json.loads(out)
    except ValueError:
        flat = {}
    result = {"User": {}, "Machine": {}, "Process": {}}
    for scope in ("User", "Machine", "Process"):
        for name in ENV_NAMES:
            if scope == "Process":
                result[scope][name] = os.environ.get(name) or None
            else:
                result[scope][name] = flat.get(f"{scope}_{name}") or None
    return result


def winhttp() -> str:
    return run_cmd(["netsh", "winhttp", "show", "proxy"]).strip()


def collect() -> dict:
    return {"wininet": read_wininet(), "env": env_vars(), "winhttp": winhttp()}
