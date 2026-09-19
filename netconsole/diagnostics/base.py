"""诊断器底层工具：PowerShell/命令执行、TCP 与 HTTP 探测。

只读原则：本模块所有函数不写注册表、不改环境变量、不杀进程。
静默原则：所有子进程一律 CREATE_NO_WINDOW，从源头消除定时检查的黑窗；
不依赖 -WindowStyle Hidden（部分终端下不可靠）。
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

CREATE_NO_WINDOW = 0x08000000


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def run_ps(script: str, timeout: int = 30) -> str:
    """运行 PowerShell 并以 UTF-8 取回输出（无窗口）。"""
    full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", full],
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return _decode(proc.stdout)


def run_cmd(args: list[str], timeout: int = 15) -> str:
    """运行本地命令（如 netsh），GBK/UTF-8 自适应解码（无窗口）。"""
    proc = subprocess.run(
        args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
    )
    return _decode(proc.stdout)


def ps_json(script: str, timeout: int = 30):
    out = run_ps(script, timeout=timeout)
    return json.loads(out)


def tcp_probe(host: str, port: int, timeout: float = 1.5) -> dict:
    """端口可连接只证明本地监听存在，不能证明节点/认证/模型可用。"""
    t0 = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        ms = round((time.perf_counter() - t0) * 1000)
        return {"up": True, "ms": ms}
    except OSError as exc:
        return {"up": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()


# 直连 opener：绕过一切系统/环境代理，用于探测底层网络本身。
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 轻量探针地址：204 表示无劫持、直连外网正常。
GEN204_URL = "http://connect.rom.miui.com/generate_204"


def http_get(url: str, timeout: float = 6.0, opener=None) -> dict:
    """GET 探测。HTTPError（如 404）也算收到了 HTTP 响应，返回 ok=True。"""
    op = opener if opener is not None else DIRECT_OPENER
    t0 = time.perf_counter()
    try:
        with op.open(url, timeout=timeout) as resp:
            body = resp.read(256)
            return {
                "ok": True,
                "status": resp.status,
                "ms": round((time.perf_counter() - t0) * 1000),
                "body_head": body[:80].decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": True,
            "status": exc.code,
            "ms": round((time.perf_counter() - t0) * 1000),
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:  # 超时/连接失败/代理故障等
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "ms": round((time.perf_counter() - t0) * 1000),
        }


def resolve(host: str) -> dict:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)
        ips = sorted({info[4][0] for info in infos})
        return {"ok": True, "ips": ips, "ms": round((time.perf_counter() - t0) * 1000)}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
