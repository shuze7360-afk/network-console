"""MCP 服务器（公开版，stdio）：只读状态查询 + 受限修复动作。

工具：
- net_status  四链路状态、摘要、待处理事项、系统代理摘要、版本；
- net_fix     受限修复：stale_proxy（受控残留清理）/ restore_bypass（直连例外合并）/
              restore_snapshot（恢复健康快照；代理功能关闭时拒绝）。

不提供任何会话内启动/停止服务的能力；权限边界与桌面一致。
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

CONSOLE_VERSION = "1.0.0"
mcp = FastMCP("network-console")
executor = Executor()
_log = get_logger("netconsole.mcp")
_log.info("mcp server start version=%s file=%s", CONSOLE_VERSION, Path(__file__).resolve())


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


@mcp.tool()
def net_status() -> str:
    """只读诊断：基础网络、网络认证、代理连接、本地服务四链路状态。

    返回 summary（一句话结论与有效时间，可直接转述给用户）、pending（待处理
    事项及首选动作说明）、features（各功能启用意图与实际状态——区分「没开」
    和「坏了」）、系统代理摘要与版本号。耗时约 5-30 秒。
    注意：功能被用户关闭时对应链路显示 主动关闭/外部已启动，这不是故障。
    """
    snap = inventory.collect_all()
    links = {
        LINK_TITLES[key]: {
            "status": snap["links"].get(key, {}).get("status"),
            "reason": snap["links"].get(key, {}).get("reason"),
        }
        for key in LINK_ORDER
    }
    proxy = snap.get("proxy_state", {}).get("wininet", {})
    return _dump({
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
    })


@mcp.tool()
def net_fix(action: str) -> str:
    """受限修复动作（经统一执行器审计，写前读旧值、写后回读）。

    action 取值：
      cleanup_stale_proxy  清理指向「受控代理端点」的死代理残留；指向其他
                           地址的代理一律不修改
      restore_bypass       按配置合并直连例外（不覆盖用户已有条目）
      restore_snapshot     恢复最近一次健康系统代理快照（代理功能关闭时拒绝）
    这些都是恢复性动作，不会启动、停止任何服务，也不修改其他配置。
    """
    actions = {
        "cleanup_stale_proxy": executor.cleanup_stale_proxy,
        "restore_bypass": executor.restore_bypass,
        "restore_snapshot": executor.restore_snapshot,
    }
    if action not in actions:
        return _dump({"ok": False, "error": f"未知 action={action}；可用：{sorted(actions)}"})
    result = actions[action]()
    return _dump(result.to_dict())


if __name__ == "__main__":
    mcp.run()
