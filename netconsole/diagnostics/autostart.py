"""自启动与计划任务采集：找出现存的三类“自动改设置”的入口。"""
from __future__ import annotations

import os
from pathlib import Path

from .base import run_ps

_PS_TASKS = (
    "Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | "
    "ForEach-Object { [pscustomobject]@{ path=$_.TaskPath; name=$_.TaskName; "
    "state=[int]$_.State; "
    "actions=(($_.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' | ') } } | "
    "ConvertTo-Json -Depth 3"
)

STATE_NAMES = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}


def _as_list(data) -> list:
    if isinstance(data, dict):
        return [data]
    return data or []


def _run_keys(hive: str) -> dict:
    out = run_ps(
        f"(Get-ItemProperty '{hive}' -ErrorAction SilentlyContinue | "
        "Select-Object * -ExcludeProperty PS*) | ConvertTo-Json -Depth 2"
    )
    try:
        import json

        return json.loads(out) or {}
    except Exception:
        return {"_raw": out[:200]}


def _startup_files(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(str(p) for p in folder.iterdir() if p.is_file())


def collect() -> dict:
    import json

    try:
        tasks = _as_list(json.loads(run_ps(_PS_TASKS)))
        for t in tasks:
            if "state" in t:
                t["state_name"] = STATE_NAMES.get(t["state"], str(t["state"]))
    except Exception as exc:
        tasks = [{"error": f"{type(exc).__name__}: {exc}"}]

    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")

    return {
        "tasks": tasks,
        "run_hkcu": _run_keys(r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"),
        "run_hklm": _run_keys(r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"),
        "startup_user": _startup_files(Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"),
        "startup_common": _startup_files(Path(programdata) / "Microsoft/Windows/Start Menu/Programs/StartUp"),
    }
