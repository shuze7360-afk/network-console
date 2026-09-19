"""功能链路探测：全部由配置驱动，公开版不含任何内置地址或名称。

- basic：直连探针（URL + 期望状态码）；
- auth：网络认证——按 默认路由网络标识的前缀 判断适用性，可达性探测登录地址；
- proxy：代理连接——端点监听 + 经代理的转发探针；关闭态检查受控残留；
- service：本地服务——端点监听 + 可选健康路径 HTTP 检查。
"""
from __future__ import annotations

import time
import urllib.request

from ..appconfig import feature
from ..models import AUTH, BASIC, PROXY, SERVICE, CheckResult, Status
from .base import DIRECT_OPENER, GEN204_URL, http_get, resolve, tcp_probe
from .net_state import connection_identity, read_hosts


def _probe_url(url: str, ok_status: int, timeout: float = 8.0) -> dict:
    d = http_get(url, timeout=timeout, opener=DIRECT_OPENER)
    d["ok"] = d.get("status") == ok_status
    return d


def probe_basic(cfg: dict) -> CheckResult:
    url = cfg.get("probe_url") or GEN204_URL
    ok_status = int(cfg.get("ok_status", 204))
    dns = resolve(url.split("/")[2].split(":")[0])
    probe = _probe_url(url, ok_status)
    if not dns["ok"]:
        return CheckResult(BASIC, Status.FAIL, f"DNS 解析失败：{dns.get('error')}",
                           evidence={"dns": dns}, kind="dns")
    if probe["ok"]:
        return CheckResult(BASIC, Status.OK, "直连探针通过", evidence={"probe": probe})
    return CheckResult(BASIC, Status.FAIL,
                       f"直连探针未通过（HTTP {probe.get('status', probe.get('error'))}）",
                       evidence={"probe": probe}, kind="basic_direct")


def probe_auth(cfg: dict, identity: dict) -> CheckResult:
    prefixes = [str(p).upper() for p in cfg.get("match_prefixes", []) if p]
    name = (identity.get("ssid") or identity.get("profile") or "").upper()
    if prefixes and name and not any(name.startswith(p) for p in prefixes):
        return CheckResult(AUTH, Status.NOT_APPLICABLE,
                           f"当前使用其他网络（{identity.get('ssid') or identity.get('profile')}），"
                           "跳过认证探测",
                           evidence={"identity": identity})
    if not prefixes and not name:
        return CheckResult(AUTH, Status.UNVERIFIED, "当前网络无法确认，不强行诊断",
                           evidence={"identity": identity})
    login_url = cfg.get("login_url", "")
    if not login_url:
        return CheckResult(AUTH, Status.UNVERIFIED, "未配置认证地址，无法探测",
                           evidence={"config": False})
    resp = http_get(login_url, timeout=8.0, opener=DIRECT_OPENER)
    expect_hosts = cfg.get("hosts_expect", {})
    hosts = read_hosts()
    hosts_ok = all(hosts.get(h) == ip for h, ip in expect_hosts.items())
    if resp.get("ok"):
        text = "认证页可达"
        if hosts_ok and expect_hosts:
            text += "；固定域名解析正常"
        return CheckResult(AUTH, Status.OK, text + "（是否已登录以页面为准）",
                           evidence={"resp": {k: resp.get(k) for k in ("status", "ms")}, "hosts_ok": hosts_ok})
    return CheckResult(AUTH, Status.FAIL,
                       f"认证页不可达（{resp.get('error') or resp.get('status')}）——可能需要登录",
                       evidence={"resp": {k: resp.get(k) for k in ("status", "error")}},
                       kind="auth")


def probe_proxy(cfg: dict, wininet: dict, env_user: dict) -> CheckResult:
    endpoint = cfg.get("endpoint", "127.0.0.1:8080")
    host, _, port_s = endpoint.rpartition(":")
    port = int(port_s) if port_s.isdigit() else 8080
    tcp = tcp_probe(host or "127.0.0.1", port, timeout=1.5)
    evidence = {"endpoint": endpoint, "tcp": tcp}
    if tcp["up"]:
        probe_url = cfg.get("probe_url") or GEN204_URL
        ok_status = int(cfg.get("ok_status", 204))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://{endpoint}"}))
        fwd = http_get(probe_url, timeout=10.0, opener=opener)
        evidence["forward"] = fwd
        if fwd.get("status") == ok_status:
            return CheckResult(PROXY, Status.OK,
                               f"代理转发正常（探针 {fwd.get('ms')}ms）", evidence=evidence)
        if fwd.get("ok"):
            return CheckResult(PROXY, Status.FAIL,
                               f"代理已启动，但转发探针未通过（HTTP {fwd.get('status')}）",
                               evidence=evidence, kind="forward")
        return CheckResult(PROXY, Status.FAIL,
                           f"代理端口在监听但转发失败：{fwd.get('error')}",
                           evidence=evidence, kind="forward")
    if wininet.get("ProxyEnable") and wininet.get("ProxyServer", "").find(str(port)) != -1:
        return CheckResult(PROXY, Status.FAIL,
                           "端口未监听，但系统代理仍指向该端口（残留，会导致浏览器断网）",
                           evidence=evidence, kind="stale_residue")
    if env_user.get("HTTP_PROXY") and str(env_user.get("HTTP_PROXY")).find(str(port)) != -1:
        return CheckResult(PROXY, Status.FAIL,
                           "端口未监听，但用户环境变量仍指向该端口",
                           evidence=evidence, kind="stale_residue")
    return CheckResult(PROXY, Status.DISABLED, "代理未运行，无残留", evidence=evidence)


def probe_service(cfg: dict) -> CheckResult:
    endpoint = cfg.get("endpoint", "127.0.0.1:9000")
    host, _, port_s = endpoint.rpartition(":")
    port = int(port_s) if port_s.isdigit() else 9000
    tcp = tcp_probe(host or "127.0.0.1", port, timeout=1.5)
    if not tcp["up"]:
        return CheckResult(SERVICE, Status.DISABLED, "本地服务未运行", evidence={"tcp": tcp})
    health = cfg.get("health_path", "")
    if health:
        resp = http_get(f"http://{endpoint}{health}", timeout=6.0, opener=DIRECT_OPENER)
        if resp.get("ok") and (resp.get("status") or 500) < 500:
            return CheckResult(SERVICE, Status.OK,
                               f"服务在线（健康检查 HTTP {resp.get('status')}）",
                               evidence={"tcp": tcp, "health": resp.get("status")})
        return CheckResult(SERVICE, Status.FAIL,
                           "端口在监听但健康检查未通过", evidence={"tcp": tcp, "health": resp})
    return CheckResult(SERVICE, Status.OK, "服务端口在监听", evidence={"tcp": tcp})


def collect_links(cfg: dict, wininet: dict, env_user: dict, extra_ports: list[int] | None = None):
    """按配置探测全部功能链路。返回 {link: CheckResult.to_dict()}。"""
    feats = cfg.get("features", {})
    out = {}
    t0 = time.perf_counter()
    try:
        out[BASIC] = probe_basic(feats.get("basic", {})).to_dict()
    except Exception as exc:  # noqa: BLE001
        out[BASIC] = CheckResult.unverified(BASIC, f"诊断器异常：{exc}").to_dict()
    out[BASIC]["probe_ms"] = round((time.perf_counter() - t0) * 1000)

    identity = connection_identity()

    t0 = time.perf_counter()
    auth_cfg = feats.get("auth", {})
    if auth_cfg.get("enabled"):
        try:
            out[AUTH] = probe_auth(auth_cfg, identity).to_dict()
        except Exception as exc:  # noqa: BLE001
            out[AUTH] = CheckResult.unverified(AUTH, f"诊断器异常：{exc}").to_dict()
    else:
        out[AUTH] = CheckResult(AUTH, Status.DISABLED, "网络认证功能未启用").to_dict()
    out[AUTH]["probe_ms"] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    proxy_cfg = feats.get("proxy", {})
    if proxy_cfg.get("enabled"):
        try:
            out[PROXY] = probe_proxy(proxy_cfg, wininet, env_user).to_dict()
        except Exception as exc:  # noqa: BLE001
            out[PROXY] = CheckResult.unverified(PROXY, f"诊断器异常：{exc}").to_dict()
    else:
        out[PROXY] = CheckResult(PROXY, Status.DISABLED, "代理连接功能未启用").to_dict()
    out[PROXY]["probe_ms"] = round((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    svc_cfg = feats.get("service", {})
    if svc_cfg.get("enabled"):
        try:
            out[SERVICE] = probe_service(svc_cfg).to_dict()
        except Exception as exc:  # noqa: BLE001
            out[SERVICE] = CheckResult.unverified(SERVICE, f"诊断器异常：{exc}").to_dict()
    else:
        out[SERVICE] = CheckResult(SERVICE, Status.DISABLED, "本地服务功能未启用").to_dict()
    out[SERVICE]["probe_ms"] = round((time.perf_counter() - t0) * 1000)

    if extra_ports:
        from .processes import listening_ports

        out["_listeners"] = listening_ports(extra_ports)
    return out
