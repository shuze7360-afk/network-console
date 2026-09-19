"""代理地址精确解析：归属判断只用解析后的 (host, port)，禁止子串/前缀匹配。

WinINET ProxyServer 有两种形态：
  "127.0.0.1:8080"
  "http=127.0.0.1:8080;https=127.0.0.1:8080;ftp=127.0.0.1:8080"
环境变量（HTTP_PROXY 等）形如 "http://127.0.0.1:8080" 或 "127.0.0.1:8080"。
历史教训：按 ":{port}" in v 或 startswith 判断归属会把 127.0.0.1:18080、
"127.0.0.1:80806" 之类误判为受控。所有归属判断必须收敛到本模块。
受控端点来自用户配置（cleanup.controlled_endpoint），本模块不内置任何端口。
"""
from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def parse_wininet_server(server: str) -> set[tuple[str, int]]:
    """解析 WinINET ProxyServer 为 (host, port) 集合；无法解析的片段丢弃。"""
    out: set[tuple[str, int]] = set()
    for part in str(server or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            part = part.rsplit("=", 1)[1].strip()
        host, _, port = part.rpartition(":")
        host = host.strip("[]").lower()
        if host and port.isdigit() and int(port) <= 65535:
            out.add((host, int(port)))
    return out


def parse_env_proxy(value: str) -> set[tuple[str, int]]:
    """解析环境变量代理值（空格/分号分隔多项均可）。"""
    out: set[tuple[str, int]] = set()
    for part in str(value or "").replace(" ", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "://" not in part:
            part = "http://" + part
        try:
            u = urlparse(part)
            port = u.port  # 超范围端口（如 180800）此处抛 ValueError
            if u.hostname and port:
                out.add((u.hostname.strip("[]").lower(), port))
        except ValueError:
            continue
    return out


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    host, _, port = str(endpoint or "").strip().rpartition(":")
    host = host.strip("[]").lower() or "127.0.0.1"
    return host, int(port) if port.isdigit() else 0


def _is_ours(eps: set[tuple[str, int]], endpoint: str) -> bool:
    """空集合（代理开启但无地址等退化态）视为受控可清理；
    非空时要求全部端点都指向受控端点——混合指向其他代理一律不算受控。"""
    if not eps:
        return True
    host, port = _parse_endpoint(endpoint)
    ours = {(h, port) for h in (_LOOPBACK | {host})}
    return eps <= ours


def wininet_points_at_controlled(server: str, endpoint: str) -> bool:
    """WinINET ProxyServer 是否（仅）指向受控端点——清理权限判定（空=退化态可清）。"""
    return _is_ours(parse_wininet_server(server), endpoint)


def wininet_references_controlled(server: str, endpoint: str) -> bool:
    """WinINET ProxyServer 是否引用了受控端点——状态展示判定（严格：空串不算）。"""
    eps = parse_wininet_server(server)
    host, port = _parse_endpoint(endpoint)
    return any(h in (_LOOPBACK | {host}) and p == port for h, p in eps)


def env_references_controlled(value: str, endpoint: str) -> bool:
    """环境变量代理是否指向受控端点（任一端点命中即视为引用了死代理）。"""
    eps = parse_env_proxy(value)
    host, port = _parse_endpoint(endpoint)
    return any(h in (_LOOPBACK | {host}) and p == port for h, p in eps)
