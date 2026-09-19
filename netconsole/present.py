"""展示层：一份状态，多处展示（首页摘要 / 待处理事项 / 操作反馈）。

纯逻辑、可离线测试。动作使用固定标识，由界面绑定到具体行为；
故障解释只陈述有证据的部分，定位不了就明说。
"""
from __future__ import annotations

import time

from .models import AUTH, BASIC, LINK_ORDER, LINK_TITLES, PROXY, SERVICE

CHECK_STALE_AFTER = 360  # 秒：两个检查周期

# 动作标识（界面绑定行为）
ACTION_OPEN_AUTH = "open_auth_page"
ACTION_RECHECK = "recheck"
ACTION_OPEN_PROGRAM = "open_program"
ACTION_SHOW_DETAILS = "show_details"
ACTION_SHOW_SERVICE = "show_service_card"

# 结构化类型 → 用户可读说明 + 首选动作（label）
EXPLANATIONS = {
    "auth": {"text": "网络认证需要登录", "action": (ACTION_OPEN_AUTH, "打开认证页")},
    "forward": {"text": "代理已启动，但转发检查未通过",
                "action": (ACTION_OPEN_PROGRAM, "打开代理程序")},
    "service_health": {"text": "本地服务没有正常响应",
                       "action": (ACTION_SHOW_SERVICE, "查看本地服务")},
    "stale_residue": {"text": "受控代理已关闭，但系统代理仍有残留指向",
                      "action": (ACTION_RECHECK, "重新检查")},
    "basic_direct": {"text": "直连外网探针未通过",
                     "action": (ACTION_RECHECK, "重新检查")},
    "dns": {"text": "域名解析异常", "action": (ACTION_RECHECK, "重新检查")},
    "generic": {"text": "原因尚未确认（完整信息见详情）",
                "action": (ACTION_SHOW_DETAILS, "查看详情")},
}


def explanation(kind: str | None) -> dict:
    return EXPLANATIONS.get(kind or "", EXPLANATIONS["generic"])


def pending_items(snap: dict) -> list[dict]:
    """待处理事项：仅统计「故障」链路；关闭/不适用/外部启动不算问题。"""
    items: list[dict] = []
    for key in LINK_ORDER:
        r = snap.get("links", {}).get(key) or {}
        if r.get("status") != "故障":
            continue
        kind = r.get("kind") or "generic"
        exp = explanation(kind)
        act = exp["action"] or (ACTION_SHOW_DETAILS, "查看详情")
        items.append({"key": key, "title": LINK_TITLES.get(key, key),
                      "text": exp["text"], "detail": r.get("reason", ""),
                      "action_id": act[0], "action_label": act[1]})
    return items


def summarize(snap: dict, now: float | None = None) -> dict:
    """首页结论。level: ok / attention / pending。"""
    now = time.time() if now is None else now
    links = snap.get("links", {})
    generated = snap.get("generated_at", 0)
    network = (snap.get("identity", {}) or {}).get("name")

    if not links or snap.get("check_failed"):
        return {"level": "pending", "text": "当前状态待确认", "network": network, "at": generated}
    if (now - generated) > CHECK_STALE_AFTER:
        return {"level": "pending",
                "text": (f"当前状态待确认（上次检查 {time.strftime('%H:%M', time.localtime(generated))}，"
                         "结果已过期）"),
                "network": network, "at": generated}

    basic_ok = links.get(BASIC, {}).get("status") == "正常"
    proxy_ok = links.get(PROXY, {}).get("status") in ("正常", "外部已启动")

    issues = []
    for key in LINK_ORDER:
        if key == BASIC:
            continue
        if links.get(key, {}).get("status") == "故障":
            issues.append(LINK_TITLES.get(key, key))

    if basic_ok and not issues:
        text = f"当前网络可用 · 已连接 {network or '网络'}" if network else "当前网络可用"
        level = "ok"
    elif not basic_ok and proxy_ok:
        text = "直连异常，代理通道可用"
        level = "attention"
    elif basic_ok and issues:
        text = "网络可用，" + "、".join(issues) + "需要处理"
        level = "attention"
    else:
        text = "当前网络不可用"
        level = "attention"
    return {"level": level, "text": text, "network": network, "at": generated}


def operation_feedback(ok: bool, decision: str, reason: str = "") -> dict:
    """操作结果 → 状态 + 核验说明 + 明确的下一步。"""
    if ok:
        state = "无需处理" if decision == "noop" else "已完成"
        verify = "连接已复查" if decision in ("cleaned", "started", "closed", "stopped", "merged") else "仍待验证"
        return {"state": state, "verify": verify, "steps": []}
    steps_map = {
        "not-configured": [{"action": ACTION_SHOW_DETAILS, "label": "查看配置说明"}],
        "elevation-declined": [{"action": ACTION_RECHECK, "label": "重新操作并确认 UAC 提示"}],
        "failed": [{"action": ACTION_SHOW_DETAILS, "label": "查看详情"}],
        "incomplete": [{"action": ACTION_RECHECK, "label": "重新检查"},
                       {"action": ACTION_SHOW_DETAILS, "label": "查看未完成项"}],
        "timeout": [{"action": ACTION_RECHECK, "label": "稍后重新检查"}],
        "port-conflict": [{"action": ACTION_SHOW_DETAILS, "label": "查看占用详情"}],
        "state-unconfirmed": [{"action": ACTION_SHOW_DETAILS, "label": "先修正配置文件"}],
        "feature-disabled": [{"action": ACTION_SHOW_DETAILS, "label": "该功能已被关闭"}],
    }
    steps = steps_map.get(decision, [{"action": ACTION_RECHECK, "label": "重新检查"}])
    return {"state": "操作冲突" if decision == "busy" else "未完成",
            "verify": "仍待验证", "steps": steps}
