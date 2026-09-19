"""MCP 服务器协议冒烟（2.0.0）：initialize → tools/list → 真实 tools/call。

以行分隔 JSON-RPC 直连 stdio，验证协议与工具语义：
- net_status：真实只读诊断，返回版本 2.0.0 与四链路结构；
- net_fix：任何合法 action 都必须返回 manual-required（不执行网络修改）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SERVER = PROJECT / "netconsole" / "mcp_server.py"
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    proc = subprocess.Popen(
        [str(PYTHON), str(SERVER)], cwd=str(PROJECT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", creationflags=0x08000000,
    )
    queue: list[tuple[str, str]] = []

    def reader() -> None:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            queue.append(("out", line))

    threading.Thread(target=reader, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_until(pred, timeout: float) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            for i, (stream, line) in enumerate(list(queue)):
                if stream != "out":
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if pred(msg):
                    queue.pop(i)
                    return msg
            time.sleep(0.1)
        return None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "smoke", "version": "0"}}})
    init = wait_until(lambda m: m.get("id") == 1, 20)
    print("initialize:", "OK" if init else "FAIL", (init or {}).get("result", {}).get("serverInfo"))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = wait_until(lambda m: m.get("id") == 2, 15)
    names = [t["name"] for t in (tools or {}).get("result", {}).get("tools", [])]
    print("tools:", names)

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "net_status", "arguments": {}}})
    status = wait_until(lambda m: m.get("id") == 3, 90)
    payload = json.loads(((status or {}).get("result", {}).get("content") or [{}])[0].get("text", "{}"))
    print("net_status links:")
    for title, info in payload.get("links", {}).items():
        print(f"  {title}: {info.get('status')} — {info.get('reason')}")
    print("system_proxy:", payload.get("system_proxy"))
    version_ok = payload.get("console_version") == "2.0.0"
    print("version 2.0.0:", version_ok)

    # net_fix 必须是 manual-required + 指引（2.0.0 核心语义）
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "net_fix", "arguments": {"action": "cleanup_stale_proxy"}}})
    fix = wait_until(lambda m: m.get("id") == 4, 30)
    fix_res = json.loads(((fix or {}).get("result", {}).get("content") or [{}])[0].get("text", "{}"))
    print("net_fix(cleanup_stale_proxy):", fix_res.get("decision"), "| guidance:",
          bool(fix_res.get("guidance")))

    send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
          "params": {"name": "net_fix", "arguments": {"action": "not_an_action"}}})
    bad = wait_until(lambda m: m.get("id") == 5, 15)
    bad_res = json.loads(((bad or {}).get("result", {}).get("content") or [{}])[0].get("text", "{}"))
    print("net_fix(未知 action):", bad_res.get("error", "")[:40])

    proc.terminate()
    ok = (init and set(names) >= {"net_status", "net_fix"} and payload.get("links")
          and version_ok and fix_res.get("decision") == "manual-required"
          and fix_res.get("guidance") and "未知 action" in bad_res.get("error", ""))
    print("\n冒烟结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
