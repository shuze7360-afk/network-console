"""通知策略对抗模拟：用伪造的检查序列离线验证静默规则（不碰真实网络）。"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole.notify_policy import NotifyPolicy  # noqa: E402

T0 = 1_000_000.0  # 测试基准时间（秒）
TMP = Path(tempfile.mkdtemp(prefix="notifypolicy-"))
RESULTS: list[tuple[str, bool, str]] = []


def fail(link: str, reason: str) -> dict:
    return {"status": "故障", "reason": reason, "checked_at": time.time()}


def ok(link: str = "proxy") -> dict:
    return {"status": "正常", "reason": "一切正常", "checked_at": time.time()}


def snap(daily=None, **others) -> dict:
    links = {"proxy": daily if daily is not None else ok()}
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
        p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF")), "auto")  # 第1轮失败：静默
        return len(p.observe(snap(ok()), "auto")) == 0  # 恢复：静默

    def c2_持续故障只提醒一次(p):
        first = rounds(p, 2)   # 第2轮提醒
        more = rounds(p, 5)    # 之后全部静默
        return first == 1 and more == 0

    def c3_自动修复失败提醒一次(p):
        p.observe(snap(fail("proxy", "端口未监听，但系统代理仍指向代理端口（残留，会导致浏览器断网）")), "auto")  # 第1轮发现残留→触发自动清理
        p.note_autofix("proxy", False, "exception", "boom")  # 清理失败
        n = p.observe(snap(fail("proxy", "端口未监听，但系统代理仍指向代理端口（残留，会导致浏览器断网）")), "auto")  # 复查仍故障
        more = p.observe(snap(fail("proxy", "端口未监听，但系统代理仍指向代理端口（残留，会导致浏览器断网）")), "auto")
        return len(n) == 1 and "自动修复" in n[0]["why"] and len(more) == 0

    def c4_自动修复成功完全安静(p):
        p.observe(snap(fail("proxy", "端口在监听但转发失败：EOF（死代理或节点不可用）")), "auto")
        p.note_autofix("proxy", True, "cleaned", "")
        return len(p.observe(snap(ok()), "auto")) == 0

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
        rounds(p, 2, base=T0)  # forward 事件已提醒（T0）
        p.observe(snap({"status": "故障", "reason": "端口未监听，但系统代理仍指向代理端口（残留）",
                        "kind": "stale_residue"}), "auto", now=T0 + 1000)  # 类型变化轮1
        n = p.observe(snap({"status": "故障", "reason": "端口未监听，但系统代理仍指向代理端口（残留）",
                            "kind": "stale_residue"}), "auto", now=T0 + 2000)  # 轮2 → 再提醒
        return len(n) == 1  # 冷却(15分钟)已过 → 新事件可提醒

    def c7_重启不重复提醒_结束后复发可再提醒(p):
        rounds(p, 2, base=T0)  # 提醒一次
        p2 = NotifyPolicy(state_path=p.state_path)  # 模拟重启（同一状态文件）
        more = rounds(p2, 3, base=T0 + 600)  # 未解决的旧事件不重复提醒
        p2.observe(snap(ok()), "auto", now=T0 + 600)
        p2.observe(snap(ok()), "auto", now=T0 + 900)  # 连续2轮恢复 → 事件结束
        again = rounds(p2, 2, base=T0 + 2000)  # 复发（冷却已过）→ 允许再次提醒
        return more == 0 and again == 1

    def c8_手动来源不弹通知但不消耗自动提醒(p):
        f = {"status": "故障", "reason": "端口在监听但转发失败：EOF"}
        p.observe({"proxy": f}, "manual")              # 轮1
        n_manual = p.observe({"proxy": f}, "manual")   # 轮2：达阈值但手动来源 → 不发也不置位
        n_auto = p.observe({"proxy": f}, "auto")       # 轮3：自动来源 → 提醒一次
        n_more = p.observe({"proxy": f}, "auto")       # 轮4：静默
        return len(n_manual) == 0 and len(n_auto) == 1 and len(n_more) == 0

    def c9_主动关闭状态不算故障(p):
        p.observe(snap({"status": "故障", "reason": "残留导致断网"}), "auto")
        p.observe(snap({"status": "主动关闭", "reason": "受控代理未运行"}), "auto")
        p.observe(snap({"status": "主动关闭", "reason": "受控代理未运行"}), "auto")
        return len(p.events) == 0  # 事件按恢复闭合

    case("C1 短暂故障下一轮恢复：全程静默", c1_短暂故障不提醒)
    case("C2 持续故障：第2轮提醒一次后静默", c2_持续故障只提醒一次)
    case("C3 自动修复失败：复查后提醒一次", c3_自动修复失败提醒一次)
    case("C4 自动修复成功：完全安静", c4_自动修复成功完全安静)
    case("C5 检查器连续两轮失败：提醒一次并可在恢复后再提醒", c5_检查器两轮失败提醒一次)
    case("C6 故障类型变化：视为新事件再提醒", c6_故障类型变化是新事件)
    case("C7 重启不重复提醒；事件闭合后复发可再提醒", c7_重启不重复提醒_结束后复发可再提醒)
    case("C8 手动来源不弹通知但不消耗自动提醒", c8_手动来源不弹通知但不消耗自动提醒)
    case("C9 主动关闭不视为故障（事件闭合）", c9_主动关闭状态不算故障)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
