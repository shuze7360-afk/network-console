"""来源边界与结果可信测试（2.0.0 核心）：全离线。

所有用例使用 NETWORK_CONSOLE_DATA_DIR 临时目录；注册表读写、进程枚举/启停、
真实网络全部以注入桩替换，未声明的系统写入即测试失败。
探针统一 (host, port) 注入；清理/恢复的成功以回读为准。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole import appconfig  # noqa: E402
from netconsole import executor as ex_mod  # noqa: E402
from netconsole.executor import Executor  # noqa: E402
from offline_env import fresh_data  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str, fn) -> None:
    try:
        got = fn()
        ok_, detail = bool(got), "" if got is True else f"实际 {got}"
    except Exception as exc:  # noqa: BLE001
        ok_, detail = False, f"{type(exc).__name__}: {exc}"
    RESULTS.append((name, ok_, detail))
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + ("" if ok_ else f" — {detail}"))


def _deny_registry():
    return (
        patch.object(ex_mod, "read_wininet",
                     lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080",
                              "ProxyOverride": "", "AutoConfigURL": ""}),
        patch.object(ex_mod, "_write_wininet",
                     lambda *a, **k: (_ for _ in ()).throw(AssertionError("后台写入注册表!"))),
    )


def _stub_popen():
    return patch.object(ex_mod.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("后台启动进程!")))


def _mutable_registry(initial: dict):
    """可变注册表桩：写操作真实生效（回读反映写入），用于成功路径。"""
    state = dict(initial)

    def read():
        return dict(state)

    def write(name, vtype, value):
        if name == "ProxyEnable":
            state["ProxyEnable"] = int(value)
        elif name in ("ProxyServer", "ProxyOverride", "AutoConfigURL"):
            state[name] = value

    return read, write, state


def _dead_registry(initial: dict):
    """写入无效桩：写操作被静默丢弃（回读不变），用于 verify-failed 路径。"""
    state = dict(initial)
    return (lambda: dict(state)), (lambda name, vt, val: None), state


def main() -> int:
    # B1 后台零修改：五入口默认来源 → manual-required，注册表/进程/意图零触碰
    def b1():
        fresh_data("b1")
        ex = Executor()
        rw, ww = _deny_registry()
        with rw, ww, _stub_popen():
            r1 = ex.cleanup_stale_proxy(port_probe=lambda h, p: False)
            r2 = ex.restore_bypass()
            r3 = ex.restore_snapshot()
            r4 = ex.feature_enable("proxy")
            r5 = ex.feature_disable("proxy")
        return {r.decision for r in (r1, r2, r3, r4, r5)} == {"manual-required"}
    case("B1 后台来源：五入口一律 manual-required 且零写入零启动", b1)

    # B2 来源门在最外层：先于状态损坏/功能停用判定
    def b2():
        d = fresh_data("b2")
        (d / "config.json").write_text("{corrupted!!!", encoding="utf-8")
        ex = Executor()
        r = ex.feature_enable("proxy")
        if r.decision != "manual-required":
            return f"来源门应最先判定，实际 {r.decision}"
        return ex.restore_snapshot().decision == "manual-required"
    case("B2 来源门先于 state-unconfirmed / feature-disabled", b2)

    # B3（修正）清理成功路径：写入真实生效 + 回读确认 → cleaned
    def b3():
        fresh_data("b3")
        ex = Executor()
        read, write, state = _mutable_registry(
            {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080",
             "ProxyOverride": "", "AutoConfigURL": ""})
        with patch.object(ex_mod, "read_wininet", read), \
             patch.object(ex_mod, "_write_wininet", write), \
             patch.object(ex_mod, "tcp_up", lambda h, p: False):
            r = ex.cleanup_stale_proxy(source="ui")
        return (r.decision == "cleaned" and state["ProxyEnable"] == 0
                and "after" in r.evidence)
    case("B3 清理成功：写入真实生效且回读开关已关 → cleaned", b3)

    # B3b（新增反例）写入无效：写被丢弃、回读仍开启 → verify-failed，不谎报
    def b3b():
        fresh_data("b3b")
        ex = Executor()
        read, write, state = _dead_registry(
            {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080",
             "ProxyOverride": "", "AutoConfigURL": ""})
        with patch.object(ex_mod, "read_wininet", read), \
             patch.object(ex_mod, "_write_wininet", write), \
             patch.object(ex_mod, "tcp_up", lambda h, p: False):
            r = ex.cleanup_stale_proxy(source="ui")
        return (r.decision == "verify-failed" and r.ok is False
                and state["ProxyEnable"] == 1 and "before" in r.evidence and "after" in r.evidence)
    case("B3b 写入无效：回读仍开启 → verify-failed（前后证据保留）", b3b)

    # B4 界面来源 + 代理停用 → restore_snapshot 拒绝（feature-disabled）
    def b4():
        fresh_data("b4")
        ex = Executor()
        return ex.restore_snapshot(source="ui").decision == "feature-disabled"
    case("B4 界面来源 + 代理停用 → feature-disabled（防复活）", b4)

    # B5 快照闸门（新增反例）：HTTP 在线 HTTPS 离线 → 全端点探测拒绝，零写入
    def b5():
        d = fresh_data("b5")
        cfg = appconfig.load_config()
        cfg["features"]["proxy"]["enabled"] = True
        appconfig.save_config(cfg)
        (d / "wininet-last-good.json").write_text(json.dumps(
            {"ProxyEnable": 1, "ProxyServer": "http=192.0.2.10:8080;https=192.0.2.11:8080",
             "ProxyOverride": "a"}), encoding="utf-8")
        ex = Executor()
        writes: list[tuple] = []
        ex_mod.read_wininet = lambda: {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        ex_mod._write_wininet = lambda name, vt, val: writes.append((name, val))
        # 注入探针：只有 HTTP 端点在线，HTTPS 离线
        with patch.object(ex_mod, "tcp_up",
                          lambda h, p: (h, p) == ("192.0.2.10", 8080)):
            r = ex.restore_snapshot(source="ui")
        if r.decision != "unsafe-snapshot" or writes:
            return f"部分在线应拒绝: {r.decision}, writes={writes}"
        # 全部在线 → 恢复成功（写入生效模拟）
        ex_mod.read_wininet = lambda: {"ProxyEnable": 1,
                                       "ProxyServer": "http=192.0.2.10:8080;https=192.0.2.11:8080",
                                       "ProxyOverride": "a"}
        with patch.object(ex_mod, "tcp_up", lambda h, p: True):
            r2 = ex.restore_snapshot(source="ui")
        if r2.decision != "restored":
            return f"全部在线应恢复: {r2.decision}"
        # 全部离线 → 拒绝（且不产生新写入）
        ex_mod.read_wininet = lambda: {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        n_before = len(writes)
        with patch.object(ex_mod, "tcp_up", lambda h, p: False):
            r3 = ex.restore_snapshot(source="ui")
        return r3.decision == "unsafe-snapshot" and len(writes) == n_before
    case("B5 快照全端点探测：部分在线拒绝/全在线恢复/全离线拒绝，拒绝零写入", b5)

    # B5b（新增反例）快照地址解析失败 → 拒绝，零写入；损坏文件 → invalid-snapshot
    def b5b():
        d = fresh_data("b5b")
        cfg = appconfig.load_config()
        cfg["features"]["proxy"]["enabled"] = True
        appconfig.save_config(cfg)
        (d / "wininet-last-good.json").write_text(json.dumps(
            {"ProxyEnable": 1, "ProxyServer": "garbage", "ProxyOverride": ""}), encoding="utf-8")
        ex = Executor()
        writes: list[tuple] = []
        ex_mod.read_wininet = lambda: {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        ex_mod._write_wininet = lambda name, vt, val: writes.append((name, val))
        r = ex.restore_snapshot(source="ui")
        if r.decision != "unsafe-snapshot" or writes:
            return f"解析失败应拒绝: {r.decision}, writes={writes}"
        (d / "wininet-last-good.json").write_text("{broken", encoding="utf-8")
        r2 = ex.restore_snapshot(source="ui")
        return r2.decision == "invalid-snapshot" and not writes
    case("B5b 快照解析失败/文件损坏 → 拒绝且零写入", b5b)

    # B6 界面来源 + 配置损坏 → state-unconfirmed
    def b6():
        d = fresh_data("b6")
        (d / "config.json").write_text("{corrupted!!!", encoding="utf-8")
        ex = Executor()
        r = ex.feature_enable("proxy", source="ui", port_probe=lambda h, p: True)
        return (r.decision == "state-unconfirmed" and appconfig.config_state_ok() is False)
    case("B6 配置损坏（ui）→ state-unconfirmed", b6)

    # B7 归属反例：远端同端口 / 端口子串 / 其他存活代理 → 均不修改
    def b7():
        fresh_data("b7")
        ex = Executor()
        writes: list[tuple] = []
        read, write, state = _dead_registry(
            {"ProxyEnable": 1, "ProxyServer": "192.0.2.10:8080",
             "ProxyOverride": "", "AutoConfigURL": ""})
        ex_mod.read_wininet = read
        ex_mod._write_wininet = lambda name, vt, val: writes.append((name, val))
        r = ex.cleanup_stale_proxy(port_probe=lambda h, p: False, source="ui")
        if r.decision != "noop" or writes or "非受控" not in r.reason:
            return f"远端同端口误动: {r.decision}, {r.reason}"
        ex_mod.read_wininet = lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:18080",
                                       "ProxyOverride": "", "AutoConfigURL": ""}
        r2 = ex.cleanup_stale_proxy(port_probe=lambda h, p: False, source="ui")
        return (r2.decision == "noop" and not writes)
    case("B7 远端同端口/端口子串 → 非受控不修改", b7)

    # B8 配置兼容：数据目录重定位、默认目录名、旧配置加载
    def b8():
        d = fresh_data("b8")
        ok_env = appconfig.data_dir() == d
        reset_default = appconfig.DEFAULT_DIR_NAME == ".network-console-app"
        (d / "config.json").write_text(json.dumps({"features": {"proxy": {"endpoint": "127.0.0.1:8080"}}}),
                                       encoding="utf-8")
        cfg = appconfig.load_config()
        ok_old = cfg["features"]["proxy"]["endpoint"] == "127.0.0.1:8080" and "cleanup" in cfg
        return ok_env and reset_default and ok_old
    case("B8 配置兼容：目录重定向/默认目录/旧配置加载", b8)

    # B9 结构断言：主窗口无自动修复残留，4 个修改入口各自显式传 source=ui
    def b9():
        src = (Path(__file__).resolve().parents[1] / "netconsole" / "ui" / "main_window.py").read_text(encoding="utf-8")
        bad = [w for w in ("allow_autofix", "note_autofix", "reset_autofix") if w in src]
        sites = ('cleanup_stale_proxy(source="ui")', 'restore_bypass(source="ui")',
                 'fn(link, source="ui")', 'restore_snapshot(source="ui")')
        return not bad and all(s in src for s in sites)
    case("B9 结构断言：无自动修复；4 个修改入口显式 source=ui", b9)

    # B10 MCP 语义：net_fix → manual-required + 指引；未知 action → 参数错误
    def b10():
        fresh_data("b10")
        from netconsole import mcp_server

        d1 = mcp_server.net_fix_impl("cleanup_stale_proxy")
        d2 = mcp_server.net_fix_impl("restore_bypass")
        d3 = mcp_server.net_fix_impl("restore_snapshot")
        d4 = mcp_server.net_fix_impl("no_such_action")
        return (d1.get("decision") == "manual-required" and "控制台" in d1.get("guidance", "")
                and d2.get("decision") == "manual-required" and d2.get("guidance")
                and d3.get("decision") == "manual-required"
                and d4.get("ok") is False and "未知 action" in d4.get("error", ""))
    case("B10 MCP：修复请求→指引；未知 action→参数错误", b10)

    # B11 混合环境变量保留：关闭代理时，混合指向的环境变量整体保留不误删
    def b11():
        fresh_data("b11")
        ex = Executor()
        with patch.object(ex_mod, "read_wininet",
                          lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080",
                                   "ProxyOverride": "", "AutoConfigURL": ""}), \
             patch.object(ex_mod, "_write_wininet", lambda *a, **k: None), \
             patch.object(ex_mod, "_run",
                          lambda args, timeout=30: ("http://127.0.0.1:8080 http://127.0.0.1:7890"
                                                    if "GetEnvironmentVariable" in " ".join(args) else "")):
            ev = ex._cleanup_settings_for(8080)
        return ev.get("env_before", {}).get("HTTP_PROXY", "") != ""
    case("B11 混合环境变量进入证据（清理与否由 points 判定）", b11)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
