"""代理地址精确解析 v2：三态解析 + 严格归属。

设计（2.0.0 审核修正）：
- 解析三态：**empty**（真正空值）、**ok**（全部片段均为有效端点）、
  **invalid**（非空字符串中存在任何无法解析的片段）。解析失败不再被当成空地址，
  也就永远拿不到清理权限。
- 主机规范化仅限：大小写、去方括号。**不合并** localhost 与 127.0.0.1、
  不合并 IPv4 与 IPv6、不合并同一地址的不同书写形式——混合书写按不同主机处理，
  取不到清理权限（安全侧）。
- 「状态展示引用受控端点」与「修改权限」分开判断：references 只要求解析出
  受控端点（供展示）；points（清理权限）要求全部端点都是受控端点，且混合
  指向一律拒绝。
- 受控端点来自用户配置；配置本身无法解析时一律拒绝修改（安全侧）。
- 内部探针统一接收 (host, port)，由调用方注入；本模块不做网络 IO。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

EMPTY = "empty"
OK = "ok"
INVALID = "invalid"


@dataclass(frozen=True)
class ParseResult:
    kind: str  # "empty" | "ok" | "invalid"
    endpoints: frozenset[tuple[str, int]]


def _norm_host(host: str) -> str:
    """仅规范化大小写与方括号；不做本机/远端/IPv4/IPv6 合并。"""
    return host.strip("[]").lower()


def parse_wininet_server(server: str | None) -> ParseResult:
    """解析 WinINET ProxyServer。

    形态："127.0.0.1:8080" 或 "http=127.0.0.1:8080;https=127.0.0.1:8080"。
    真正空值 → empty；任何片段无法解析 → invalid（整串失效）。
    """
    text = "" if server is None else str(server)
    if not text.strip():
        return ParseResult(EMPTY, frozenset())
    endpoints: set[tuple[str, int]] = set()
    for part in text.split(";"):
        part = part.strip()
        if "=" in part:
            part = part.rsplit("=", 1)[1].strip()
        host, _, port = part.rpartition(":")
        host = _norm_host(host)
        if not host or not port.isdigit() or int(port) > 65535:
            return ParseResult(INVALID, frozenset())
        endpoints.add((host, int(port)))
    if not endpoints:
        return ParseResult(INVALID, frozenset())
    return ParseResult(OK, frozenset(endpoints))


def parse_env_proxy(value: str | None) -> ParseResult:
    """解析环境变量代理值（"http://host:port" 或空格/分号分隔多项）。"""
    text = "" if value is None else str(value)
    if not text.strip():
        return ParseResult(EMPTY, frozenset())
    endpoints: set[tuple[str, int]] = set()
    for part in text.replace(" ", ";").split(";"):
        part = part.strip()
        if not part:
            return ParseResult(INVALID, frozenset())
        if "://" not in part:
            part = "http://" + part
        try:
            u = urlparse(part)
            port = u.port  # 超范围端口（如 180800）此处抛 ValueError
            host = _norm_host(u.hostname or "")
            if not host or not port:
                return ParseResult(INVALID, frozenset())
        except ValueError:
            return ParseResult(INVALID, frozenset())
        endpoints.add((host, port))
    return ParseResult(OK, frozenset(endpoints))


def parse_endpoint(endpoint: str | None) -> tuple[str, int] | None:
    """解析受控端点配置（"host:port"）；无效返回 None（调用方必须拒绝修改）。"""
    text = "" if endpoint is None else str(endpoint)
    host, _, port = text.strip().rpartition(":")
    host = _norm_host(host)
    if not host or not port.isdigit() or int(port) > 65535:
        return None
    return host, int(port)


def wininet_points_at_controlled(server: str | None, endpoint: str | None) -> bool:
    """清理权限：真空地址（空地址残留）或全部端点恰为受控端点时为 True；
    解析失败、混合指向、受控端点配置无效一律 False。"""
    ep = parse_endpoint(endpoint)
    if ep is None:
        return False
    r = parse_wininet_server(server)
    if r.kind == EMPTY:
        return True
    if r.kind != OK:
        return False
    return r.endpoints == frozenset({ep})


def wininet_references_controlled(server: str | None, endpoint: str | None) -> bool:
    """状态展示：地址解析出受控端点即算引用（混合指向也算引用）；empty/invalid 为 False。"""
    ep = parse_endpoint(endpoint)
    if ep is None:
        return False
    r = parse_wininet_server(server)
    if r.kind != OK:
        return False
    return ep in r.endpoints


def env_points_at_controlled(value: str | None, endpoint: str | None) -> bool:
    """环境变量清理权限：值非空且全部端点均为受控端点。"""
    ep = parse_endpoint(endpoint)
    if ep is None:
        return False
    r = parse_env_proxy(value)
    if r.kind != OK:
        return False
    return r.endpoints == frozenset({ep})


def env_references_controlled(value: str | None, endpoint: str | None) -> bool:
    """环境变量展示判定：任一端点命中受控端点。"""
    ep = parse_endpoint(endpoint)
    if ep is None:
        return False
    r = parse_env_proxy(value)
    if r.kind != OK:
        return False
    return ep in r.endpoints


def probe_endpoints(server: str | None, probe) -> bool:
    """对地址中的全部端点逐一执行注入探针 probe(host, port)；全部通过才为 True。
    empty/invalid 返回 False。探针由调用方注入（测试不得触网）。"""
    r = parse_wininet_server(server)
    if r.kind != OK or not r.endpoints:
        return False
    return all(probe(host, port) for host, port in sorted(r.endpoints))
