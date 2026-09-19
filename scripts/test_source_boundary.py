"""来源边界测试（2.0.0 核心）：后台零修改 + 界面可用 + 兼容性（全离线）。

所有"离线"用例使用 NETWORK_CONSOLE_DATA_DIR 临时目录；注册表读写、进程
枚举/启停、真实网络全部以注入桩替换。未声明的系统写入即测试失败。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole import appconfig  # noqa: E402
from netconsole import executor as ex_mod  # noqa: E402
from netconsole.executor import Executor  # noqa: E402
from offline_env import fresh_data  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="source-boundary-"))
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
    """注册表桩：读返回受控代理健康态，写即抛 AssertionError（后台写入=测试失败）。"""
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


def main() -> int:
    # B1 后台零修改：五个修改入口默认来源 → manual-required，注册表/进程/意图零触碰
    def b1():
        fresh_data("b1")
        ex = Executor()
        rw, ww = _deny_registry()
        with rw, ww, _stub_popen():
            r1 = ex.cleanup_stale_proxy(port_probe=lambda p: False)
            r2 = ex.restore_bypass()
            r3 = ex.restore_snapshot()
            r4 = ex.feature_enable("proxy")
            r5 = ex.feature_disable("proxy")
        decisions = {r.decision for r in (r1, r2, r3, r4, r5)}
        return decisions == {"manual-required"}
    case("B1 后台来源：五入口一律 manual-required 且零写入零启动", b1)

    # B2 来源门在最外层：先于状态损坏/功能停用判定
    def b2():
        d = fresh_data("b2")
        (d / "config.json").write_text("{corrupted!!!", encoding="utf-8")
        ex = Executor()
        r = ex.feature_enable("proxy")
        if r.decision != "manual-required":
            return f"来源门应最先判定，实际 {r.decision}"
        # 代理功能停用时，后台恢复快照同样先撞来源门
        r2 = ex.restore_snapshot()
        return r2.decision == "manual-required"
    case("B2 来源门先于 state-unconfirmed / feature-disabled", b2)

    # B3 界面来源可用（真实业务路径，桩替注册表）
    def b3():
        fresh_data("b3")
        ex = Executor()
        with patch.object(ex_mod, "read_wininet",
                          lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080",
                                   "ProxyOverride": "", "AutoConfigURL": ""}), \
             patch.object(ex_mod, "_write_wininet", lambda *a, **k: None), \
             patch.object(ex_mod, "tcp_up", lambda p: False):
            r = ex.cleanup_stale_proxy(source="ui")
        return r.decision == "cleaned"
    case("B3 界面来源：清理正常执行并回读", b3)

    # B4 界面来源 + 代理停用 → restore_snapshot 拒绝（feature-disabled）
    def b4():
        fresh_data("b4")
        ex = Executor()
        r = ex.restore_snapshot(source="ui")
        return r.decision == "feature-disabled"
    case("B4 界面来源 + 代理停用 → feature-disabled（防复活）", b4)

    # B5 快照可用性闸门：目标不可达 → unsafe-snapshot，零写入；回读含 ProxyEnable
    def b5():
        d = fresh_data("b5")
        cfg = appconfig.load_config()
        cfg["features"]["proxy"]["enabled"] = True
        appconfig.save_config(cfg)
        (d / "wininet-last-good.json").write_text(json.dumps(
            {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8080", "ProxyOverride": "a"}),
            encoding="utf-8")
        ex = Executor()
        writes: list[tuple] = []
        real_tcp = ex_mod.tcp_up
        ex_mod.read_wininet = lambda: {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
        ex_mod._write_wininet = lambda name, vt, val: writes.append((name, val))
        ex_mod.tcp_up = lambda port, host="127.0.0.1", timeout=0.9: False
        try:
            r = ex.restore_snapshot(source="ui")
        finally:
            ex_mod.tcp_up = real_tcp
        if r.decision != "unsafe-snapshot" or writes:
            return f"拒绝语义失败: {r.decision}, writes={writes}"
        # 可恢复快照：三字段写入含 ProxyEnable
        (d / "wininet-last-good.json").write_text(json.dumps(
            {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": "a;b"}),
            encoding="utf-8")
        after = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": "a;b"}
        ex_mod.read_wininet = lambda: after
        r2 = ex.restore_snapshot(source="ui")
        names = [w[0] for w in writes]
        return (r2.decision == "restored"
                and "ProxyEnable" in names and "ProxyServer" in names and "ProxyOverride" in names)
    case("B5 快照闸门：不可达拒绝零写入；可恢复时回读含 ProxyEnable", b5)

    # B6 界面来源 + 状态损坏 → state-unconfirmed，意图未被写入
    def b6():
        d = fresh_data("b6")
        (d / "config.json").write_text("{corrupted!!!", encoding="utf-8")
        ex = Executor()
        r = ex.feature_enable("proxy", source="ui", port_probe=lambda p: True)
        return (r.decision == "state-unconfirmed"
                and appconfig.config_state_ok() is False)
    case("B6 配置损坏（ui）→ state-unconfirmed", b6)

    # B7 精确归属：非受控代理不修改；含端口子串的地址不误判
    def b7():
        fresh_data("b7")
        ex = Executor()
        writes: list[tuple] = []
        ex_mod.read_wininet = lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:18080",
                                       "ProxyOverride": "", "AutoConfigURL": ""}
        ex_mod._write_wininet = lambda name, vt, val: writes.append((name, val))
        r = ex.cleanup_stale_proxy(port_probe=lambda p: False, source="ui")
        return (r.decision == "noop" and not writes
                and any("非受控" in n for n in r.evidence.get("notes", [])))
    case("B7 其他代理（18080）不修改、不误判", b7)

    # B8 配置兼容：数据目录重定位、默认目录名、旧配置加载
    def b8():
        d = fresh_data("b8")
        ok_env = appconfig.data_dir() == d
        reset_default = appconfig.DEFAULT_DIR_NAME == ".network-console-app"
        # 最小旧配置（缺新字段）应正常加载
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

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
