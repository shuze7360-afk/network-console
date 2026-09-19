"""通用模型：四类功能链路（基础网络/网络认证/代理连接/本地服务）。"""
from __future__ import annotations

from dataclasses import dataclass, field
import time

BASIC = "basic"
AUTH = "auth"
PROXY = "proxy"
SERVICE = "service"

LINK_ORDER = [BASIC, AUTH, PROXY, SERVICE]
LINK_TITLES = {BASIC: "基础网络", AUTH: "网络认证", PROXY: "代理连接", SERVICE: "本地服务"}


class Status:
    OK = "正常"
    FAIL = "故障"
    UNVERIFIED = "未验证"
    DISABLED = "主动关闭"
    NOT_APPLICABLE = "不适用"
    EXTERNAL = "外部已启动"


@dataclass
class CheckResult:
    """一次链路探测的结果。kind 为结构化类型，供解释映射与自动处理使用。"""

    link: str
    status: str
    reason: str
    checked_at: float = field(default_factory=time.time)
    evidence: dict = field(default_factory=dict)
    kind: str | None = None

    def to_dict(self) -> dict:
        return {
            "link": self.link,
            "status": self.status,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "evidence": self.evidence,
            "kind": self.kind,
        }

    @classmethod
    def unverified(cls, link: str, reason: str) -> "CheckResult":
        return cls(link=link, status=Status.UNVERIFIED, reason=reason)
