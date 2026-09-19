"""诊断汇总：按配置探测全部功能链路 + 通用漂移发现。"""
from __future__ import annotations

import socket
import time

from . import links, proxy_state
from ..appconfig import config_state_ok, load_config
from ..models import LINK_ORDER

PORT_HINTS = {
    "proxy": "endpoint",
    "service": "endpoint",
}


def _collect_ports(cfg: dict) -> list[int]:
    ports = []
    for key in ("proxy", "service"):
        f = cfg.get("features", {}).get(key, {})
        endpoint = str(f.get("endpoint", ""))
        _, _, port = endpoint.rpartition(":")
        if port.isdigit():
            ports.append(int(port))
    return ports


def _findings(cfg: dict, links: dict, extra: dict) -> list[dict]:
    findings: list[dict] = []
    clean = cfg.get("cleanup", {})
    wininet = extra.get("wininet", {})
    env_user = extra.get("env", {}).get("User", {})
    proxy_on = cfg.get("features", {}).get("proxy", {}).get("enabled")
    controlled = str(clean.get("controlled_endpoint", ""))
    port = controlled.rpartition(":")[2] or controlled.split(":")[-1]

    if proxy_on and wininet.get("ProxyEnable") and not wininet.get("ProxyOverride"):
        findings.append({
            "level": "warn", "code": "PROXY_BYPASS_MISSING",
            "text": "系统代理已开启但直连例外列表为空——本应直连的目标会经代理转发。",
        })
    if env_user.get("HTTP_PROXY") or env_user.get("HTTPS_PROXY"):
        findings.append({
            "level": "info", "code": "ENV_PROXY_PRESENT",
            "text": "存在用户级代理环境变量（影响新启动的命令行工具）。",
        })
    wildcard = [l for l in extra.get("listeners", [])
                if str(l.get("LocalAddress", "")).startswith("0.0.0.0")]
    if wildcard:
        findings.append({
            "level": "info", "code": "LISTEN_WILDCARD",
            "text": "受管服务监听 0.0.0.0（对局域网可见）；建议配置绑定 127.0.0.1。",
        })
    if not config_state_ok():
        findings.append({
            "level": "warn", "code": "STATE_UNCONFIRMED",
            "text": "配置文件损坏不可确认——自动处理已被禁止；请修正或重置配置文件。",
        })
    return findings


def collect_all() -> dict:
    cfg = load_config()
    data: dict = {
        "generated_at": time.time(),
        "host": socket.gethostname(),
        "features": {"intent": {k: bool(cfg["features"][k].get("enabled")) for k in LINK_ORDER},
                     "config_ok": config_state_ok()},
    }

    def step(key, fn):
        try:
            data[key] = fn()
        except Exception as exc:  # noqa: BLE001
            data[key] = {"error": f"{type(exc).__name__}: {exc}"}

    step("proxy_state", proxy_state.collect)
    wininet = data.get("proxy", {}).get("wininet", {})
    env_user = data.get("proxy", {}).get("env", {}).get("User", {})
    ports = _collect_ports(cfg)
    t0 = time.perf_counter()
    try:
        from . import links as links_mod

        data["links"] = links_mod.collect_links(cfg, wininet, env_user, extra_ports=ports)
    except Exception as exc:  # noqa: BLE001
        data["links"] = {"error": f"{type(exc).__name__}: {exc}"}
    data["links_elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    links = data.get("links", {})
    env_user = data.get("proxy_state", {}).get("env", {}).get("User", {})
    wininet = data.get("proxy_state", {}).get("wininet", {})
    listeners = links.get("_listeners", [])
    data["findings"] = _findings(cfg, links,
                                 {"wininet": wininet, "env": {"User": env_user},
                                  "listeners": listeners}) if "links" in data else []
    return data
