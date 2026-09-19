"""MCP 服务器（公开版 2.0.0，stdio）：只读诊断 + 人工操作指引。

**2.0.0 行为变更（不兼容）**：后台与 AI 会话只诊断、提醒与解释；网络修改只能
由用户在控制台界面点击触发。`net_fix` 不再代为执行修复——合法 action 返回
`manual-required` 与界面操作指引（拒绝本身留审计）；工具名称与参数保持不变，
依赖旧"调用即修复"行为的集成需要适配。

工具：
- net_status  四链路状态、摘要、待处理事项、系统代理摘要、版本（只读）；
- net_fix     返回 manual-required 与人工操作指引，不执行网络修改。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from netconsole import present  # noqa: E402
from netconsole.diagnostics import inventory  # noqa: E402
from netconsole.executor import Executor  # noqa: E402
from netconsole.logutil import get_logger  # noqa: E402
from netconsole.models import LINK_ORDER, LINK_TITLES  # noqa: E402

CONSOLE_VERSION = "2.0.0"
mcp = FastMCP("network-console")
executor = Executor()
_log = get_logger("netconsole.mcp")
_log.info("mcp server start version=%s file=%s", CONSOLE_VERSION, Path(__file__).resolve())

# 人工操作指引：告诉用户去控制台哪里操作（与界面入口一一对应）。
MANUAL_GUIDANCE = {
    "cleanup_stale_proxy": "请在桌面控制台「代理连接」卡片点击「清理残留代理」；"
                           "定时检查发现问题后也会提醒你处理。",
    "restore_bypass": "请在桌面控制台「代理连接」卡片点击「恢复直连例外」"
                      "（按配置合并写入，不覆盖已有条目）。",
    "restore_snapshot": "请在桌面控制台底部点击「恢复上次配置」"
                        "（会先确认，恢复到最近健康快照）。",
}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def net_status_impl() -> dict:
    """只读诊断（与桌面同一快照，结论一致）。"""
    snap = inventory.collect_all()
    links = {
        LINK_TITLES[key]: {
            "status": snap["links"].get(key, {}).get("status"),
            "reason": snap["links"].get(key, {}).get("reason"),
        }
        for key in LINK_ORDER
    }
    proxy = snap.get("proxy_state", {}).get("wininet", {})
    return {
        "console_version": CONSOLE_VERSION,
        "summary": present.summarize(snap),
        "pending": present.pending_items(snap),
        "links": links,
        "features": snap.get("features", {}),
        "operation": {"busy": executor.is_busy},
        "system_proxy": {
            "ProxyEnable": proxy.get("ProxyEnable"),
            "ProxyServer": proxy.get("ProxyServer"),
            "ProxyOverride": proxy.get("ProxyOverride") or "(空)",
        },
        "checked_at": snap.get("generated_at"),
    }


def net_fix_impl(action: str) -> dict:
    """恢复类动作 → manual-required + 人工指引（不执行修复，拒绝本身留审计）。"""
    actions = ("cleanup_stale_proxy", "restore_bypass", "restore_snapshot")
    if action not in actions:
        return {"ok": False, "error": f"未知 action={action}；可用：{sorted(actions)}"}
    # source 默认 background → 执行器来源闸门返回 manual-required（零网络写入）。
    result = getattr(executor, action)()
    d = result.to_dict()
    if d.get("decision") == "manual-required":
        d["guidance"] = MANUAL_GUIDANCE.get(action, "请在桌面控制台界面操作。")
    return d


@mcp.tool()
def net_status() -> str:
    """只读诊断：基础网络、网络认证、代理连接、本地服务四链路状态。

    返回 summary（一句话结论与有效时间，可直接转述给用户）、pending（待处理
    事项及首选动作说明）、features（各功能启用意图与实际状态——区分「没开」
    和「坏了」）、系统代理摘要与版本号。耗时约 5-30 秒。
    注意：功能被用户关闭时对应链路显示 主动关闭/外部已启动，这不是故障。
    本工具只诊断不修改：修复请把 pending 的动作提示转述给用户，引导其在
    桌面控制台操作（AI 会话不能代用户修改网络）。
    """
    return _dump(net_status_impl())


@mcp.tool()
def net_fix(action: str) -> str:
    """恢复类动作 → 返回人工操作指引，不执行修复（2.0.0 行为变更）。

    2.0.0 起后台会话不能修改网络：调用任何合法 action 都返回
    decision="manual-required" 与 guidance（去控制台哪里点哪个按钮）。
    请把指引原文转述给用户，不要反复重试。拒绝本身留审计。
    action 取值：cleanup_stale_proxy / restore_bypass / restore_snapshot；
    其他值返回参数错误。
    """
    return _dump(net_fix_impl(action))


if __name__ == "__main__":
    mcp.run()
