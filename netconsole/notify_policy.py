"""通知策略：决定哪些观察需要提醒用户（纯逻辑，可离线测试，时间可注入）。

规则（2.0.0）：
- 故障第 1 轮只记录；同一链路同一故障类型持续到第 2 轮才提醒一次，之后不重复。
- 「已通知」只在真正发送后置位——手动检查只计数，不消耗自动提醒机会。
- 恢复永远安静；连续 2 轮「正常」闭合事件；「未验证」不计数；
  「主动关闭 / 不适用 / 外部已启动」立即作废事件。
- 同一链路两条提醒至少间隔 MIN_NOTIFY_INTERVAL（防故障抖动刷屏）；
  冷却时间戳持久化（重启后冷却仍有效）。
- **无自动修复**（2.0.0 起）：控制台只发现问题、提示操作；网络修改只能由
  用户在界面触发。原 autofix API 已随需求变更移除。
- 事件状态原子持久化：重启不重复提醒未解决旧事件。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import appconfig
from .models import LINK_ORDER

STATE_PATH = None  # 运行时解析为应用数据目录（可被 NETWORK_CONSOLE_DATA_DIR 重定位）
FAIL_ROUNDS_TO_NOTIFY = 2
RECOVER_ROUNDS_TO_CLOSE = 2
MIN_NOTIFY_INTERVAL = 900  # 同一链路两条提醒的最小间隔（15 分钟），防故障抖动刷屏
CHECKER_FAIL_ROUNDS_TO_NOTIFY = 2


def classify_failure(reason: str) -> str:
    """无结构化 kind 时从结论文本推断类型（与 present.EXPLANATIONS 同一词汇）。"""
    r = reason or ""
    if "残留" in r:
        return "stale_residue"
    if "认证" in r or "hosts" in r:
        return "auth"
    if "转发" in r or "节点" in r or "EOF" in r or "劫持" in r:
        return "forward"
    if "服务" in r or "健康" in r:
        return "service_health"
    if "DNS" in r:
        return "dns"
    if "直连" in r:
        return "basic_direct"
    return "generic"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class NotifyPolicy:
    def __init__(self, state_path: Path | None = None, logger=None) -> None:
        from . import appconfig

        self.state_path = Path(state_path) if state_path else (
            appconfig.data_dir() / "notify-state.json")
        self.log = logger
        self.events: dict[str, dict] = {}
        self.checker_fail_rounds = 0
        self.checker_notified = False
        self.last_notified_at: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.events = data.get("events", {})
            self.last_notified_at = data.get("last_notified_at", {})
            self.checker_fail_rounds = data.get("checker_fail_rounds", 0)
            self.checker_notified = data.get("checker_notified", False)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            _atomic_write(self.state_path, json.dumps({
                "events": self.events,
                "last_notified_at": self.last_notified_at,
                "checker_fail_rounds": self.checker_fail_rounds,
                "checker_notified": self.checker_notified,
            }, ensure_ascii=False, indent=1))
        except Exception:
            pass

    # ---- 每轮检查观察 ----
    def observe(self, links: dict, source: str = "auto", now: float | None = None) -> list[dict]:
        out: list[dict] = []
        now = time.time() if now is None else now
        for link in LINK_ORDER:
            r = links.get(link)
            if not r:
                continue
            title_key = link
            status = r.get("status")
            if status == "故障":
                ftype = str(r.get("kind") or "") or classify_failure(str(r.get("reason", "")))
                ev = self.events.get(title_key)
                if not ev or ev.get("type") != ftype:
                    ev = {"type": ftype, "rounds": 0, "notified": False,
                          "first_seen": now, "recover_rounds": 0}
                ev["rounds"] = int(ev.get("rounds", 0)) + 1
                ev["recover_rounds"] = 0
                ev["reason"] = str(r.get("reason", ""))
                self.events[title_key] = ev

                should, why = False, ""
                if ev["rounds"] >= FAIL_ROUNDS_TO_NOTIFY and not ev.get("notified"):
                    should = True
                    why = f"故障已持续 {ev['rounds']} 轮"
                cooled = now - self.last_notified_at.get(title_key, 0) >= MIN_NOTIFY_INTERVAL
                if should and source == "auto" and cooled:
                    self.last_notified_at[title_key] = now
                    ev["notified"] = True  # 只有真正发送后才置位
                    out.append({
                            "key": title_key, "type": ftype,
                            "title": self._title(link),
                            "message": f"{ev['reason']}（{why}）",
                            "why": why,
                        })
                    if self.log:
                        self.log.info("observe %s fail type=%s round=%s should=%s why=%s source=%s",
                                      link, ftype, ev["rounds"], should, why, source)
            else:
                ev = self.events.get(title_key)
                if status in ("主动关闭", "不适用", "外部已启动"):
                    # 功能关闭/不适用/外部启动：如实展示而非故障，待提醒事件立即作废
                    if title_key in self.events and self.log:
                        self.log.info("event discard %s (status=%s)", link, status)
                    self.events.pop(title_key, None)
                elif status == "正常" and ev:
                    ev["recover_rounds"] = int(ev.get("recover_rounds", 0)) + 1
                    if ev["recover_rounds"] >= RECOVER_ROUNDS_TO_CLOSE:
                        if self.log:
                            self.log.info("event close %s (recovered)", link)
                        self.events.pop(title_key, None)
                # 未验证/其他中间态：不算恢复，也不计失败
        self._save()
        return out

    # ---- 检查器自身健康 ----
    def observe_checker_failure(self, source: str = "auto") -> list[dict]:
        self.checker_fail_rounds += 1
        out: list[dict] = []
        if (self.checker_fail_rounds >= CHECKER_FAIL_ROUNDS_TO_NOTIFY
                and not self.checker_notified):
            self.checker_notified = True
            if source == "auto":
                out.append({
                    "key": "__checker__", "type": "checker",
                    "title": "监测暂不可用",
                    "message": f"检查器已连续 {self.checker_fail_rounds} 轮失败，暂无法判断网络状态；这不是网络故障报警。",
                    "why": "checker failed rounds>=2",
                })
        if self.log:
            self.log.info("checker fail round=%s notified=%s", self.checker_fail_rounds, self.checker_notified)
        self._save()
        return out

    def observe_checker_success(self) -> None:
        if self.checker_fail_rounds or self.checker_notified:
            if self.log:
                self.log.info("checker recovered")
        self.checker_fail_rounds = 0
        self.checker_notified = False
        self._save()

    @staticmethod
    def _title(link: str) -> str:
        from .models import LINK_TITLES

        return LINK_TITLES.get(link, link)
