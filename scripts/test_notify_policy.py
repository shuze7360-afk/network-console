"""通知策略对抗模拟（2.0.0）：用伪造的检查序列离线验证静默规则（不碰真实网络）。

变更说明（2.0.0）：
- C3/C4 原「自动修复失败/成功」用例随自动修复移除改写：
  C3 → 冷却时间戳必须持久化（重启后同链路冷却仍生效，防刷屏回归）；
  C4 → 通知标题必须是显示名（LINK_TITLES），不得出现内部标识。
- C10 新增：classify_failure 兜底词汇与 present.EXPLANATIONS 对齐。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole.models import LINK_TITLES  # noqa: E402
from netconsole.notify_policy import NotifyPolicy, classify_failure  # noqa: E402

T0 = 1_000_000.0  # 测试基准时间（秒）
TMP = Path(tempfile.mkdtemp(prefix="notifypolicy-"))
RESULTS: list[tuple[str, bool, str]] = []


def fail(link: str, reason: str) -> dict:
    return {"status": "故障", "reason": reason, "checked_at": time.time()}


def ok(link: str = "proxy") -> dict:
    return {"status": "正常", "reason": "一切正常", "checked_at": time.time()}


def snap(proxy=None, **others) -> dict:
    links = {"proxy": proxy if proxy is not None else ok()}
    links.update(others)
    return links


def case(name: str, fn) -> None:
    state = TMP / f"{name}.json"
    if state.exists():
        state.unlink()
    p = NotifyPolicy(state_path=state)
    got = fn(p)
    ok_ = got is True
    RESULTS.append((name, ok_, "" if ok_ else f"期望 True，实际 {got}"))
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" if ok_ else f"  [FAIL] {name}: 期望 True，实际 {got}")


def rounds(p: NotifyPolicy, n: int, source="auto", base: float | None = None) -> int:
    total = 0
    b = base if base is not None else T0
    for i in range(n):
        total += len(p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF（死代理或节点不可用）")),
                               source, now=b + i * 60))
    return total


def main() -> int:
    def c1_短暂故障不提醒(p):
        p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF")), "auto")
        return len(p.observe(snap(ok()), "auto")) == 0

    def c2_持续故障只提醒一次(p):
        first = rounds(p, 2)
        more = rounds(p, 5)
        return first == 1 and more == 0

    def c3_冷却时间戳持久化(p):
        rounds(p, 2, base=T0)
        p.observe(snap(ok()), "auto", now=T0 + 120)
        p.observe(snap(ok()), "auto", now=T0 + 180)
        p2 = NotifyPolicy(state_path=p.state_path)  # 模拟重启（同一状态文件）
        n1 = rounds(p2, 2, base=T0 + 480)    # 冷却期内的新事件第 2 轮：静默
        n2 = rounds(p2, 3, base=T0 + 1010)   # 冷却过后：允许提醒
        return n1 == 0 and n2 == 1

    def c4_通知标题是显示名(p):
        n = p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF")), "auto", now=T0)
        n += p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF")), "auto", now=T0 + 60)
        if len(n) != 1:
            return f"应提醒一次，实际 {len(n)}"
        item = n[0]
        return item["title"] == LINK_TITLES["proxy"] and item["title"] != "proxy"

    def c5_检查器两轮失败提醒一次(p):
        a = p.observe_checker_failure("auto")
        b = p.observe_checker_failure("auto")
        c = p.observe_checker_failure("auto")
        p.observe_checker_success()
        d = p.observe_checker_failure("auto")
        e = p.observe_checker_failure("auto")
        return len(a) == 0 and len(b) == 1 and len(c) == 0 and len(d) == 0 and len(e) == 1 \
            and "监测" in b[0]["title"]

    def c6_故障类型变化是新事件(p):
        rounds(p, 2, base=T0)
        p.observe(snap(fail("proxy", "关闭未完成：系统代理仍指向已关闭的受控代理（残留）")), "auto", now=T0 + 1000)
        n = p.observe(snap(fail("proxy", "关闭未完成：系统代理仍指向已关闭的受控代理（残留）")), "auto", now=T0 + 2000)
        return len(n) == 1

    def c7_重启不重复提醒_结束后复发可再提醒(p):
        rounds(p, 2, base=T0)
        p2 = NotifyPolicy(state_path=p.state_path)  # 模拟重启（同一状态文件）
        more = rounds(p2, 3, base=T0 + 600)
        p2.observe(snap(ok()), "auto", now=T0 + 600)
        p2.observe(snap(ok()), "auto", now=T0 + 900)
        again = rounds(p2, 2, base=T0 + 2000)
        return more == 0 and again == 1

    def c8_手动来源不弹通知但不消耗自动提醒(p):
        f = {"status": "故障", "reason": "端口在监听但转发失败：EOF"}
        p.observe({"proxy": f}, "manual")
        n_manual = p.observe({"proxy": f}, "manual")
        n_auto = p.observe({"proxy": f}, "auto")
        n_more = p.observe({"proxy": f}, "auto")
        return len(n_manual) == 0 and len(n_auto) == 1 and len(n_more) == 0

    def c9_主动关闭状态不算故障(p):
        p.observe(snap({"status": "故障", "reason": "残留导致断网"}), "auto")
        p.observe(snap({"status": "主动关闭", "reason": "受控代理未运行"}), "auto")
        p.observe(snap({"status": "主动关闭", "reason": "受控代理未运行"}), "auto")
        return len(p.events) == 0

    def c10_classify词汇与解释表对齐(p):
        from netconsole import present

        keys = set(present.EXPLANATIONS)
        samples = {
            "stale_residue": "关闭未完成：系统代理仍指向已关闭的受控代理（残留）",
            "auth": "网络认证需要登录（hosts）",
            "forward": "转发检查未通过：EOF",
            "service_health": "本地服务没有正常响应",
            "dns": "DNS 解析异常",
            "basic_direct": "直连外网探针未通过",
        }
        for want, reason in samples.items():
            if classify_failure(reason) != want:
                return f"{reason!r} → {classify_failure(reason)} ≠ {want}"
        return all(classify_failure(r) in keys for r in samples.values())

    case("C1 短暂故障下一轮恢复：全程静默", c1_短暂故障不提醒)
    case("C2 持续故障：第2轮提醒一次后静默", c2_持续故障只提醒一次)
    case("C3 冷却时间戳持久化：重启后同链路冷却仍生效", c3_冷却时间戳持久化)
    case("C4 通知标题是显示名而非内部标识", c4_通知标题是显示名)
    case("C5 检查器连续两轮失败：提醒一次并可在恢复后再提醒", c5_检查器两轮失败提醒一次)
    case("C6 故障类型变化：视为新事件再提醒", c6_故障类型变化是新事件)
    case("C7 重启不重复提醒；事件闭合后复发可再提醒", c7_重启不重复提醒_结束后复发可再提醒)
    case("C8 手动来源不弹通知但不消耗自动提醒", c8_手动来源不弹通知但不消耗自动提醒)
    case("C9 主动关闭不视为故障（事件作废）", c9_主动关闭状态不算故障)
    case("C10 classify_failure 兜底词汇与 EXPLANATIONS 对齐", c10_classify词汇与解释表对齐)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
