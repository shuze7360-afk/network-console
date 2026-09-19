"""展示层单元测试（公开版，全离线）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole import present  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
NOW = time.time()


def r(status: str, reason: str = "", ms: int = 100, kind: str | None = None) -> dict:
    return {"status": status, "reason": reason, "checked_at": NOW, "probe_ms": ms,
            "evidence": {}, "kind": kind}


def base(**kw) -> dict:
    links = {
        "basic": r("正常", "直连探针通过"),
        "auth": r("正常", "认证页可达"),
        "proxy": r("正常", "代理转发正常"),
        "service": r("正常", "服务在线"),
    }
    links.update(kw)
    return {"links": links,
            "features": {"intent": {"basic": True, "auth": True, "proxy": True, "service": True}},
            "identity": {"name": "ExampleNet"},
            "generated_at": NOW}


def case(name: str, fn) -> None:
    try:
        got = fn()
        ok_, detail = bool(got), "" if got is True else f"实际 {got}"
    except Exception as exc:  # noqa: BLE001
        ok_, detail = False, f"{type(exc).__name__}: {exc}"
    RESULTS.append((name, ok_, detail))
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + ("" if ok_ else f" — {detail}"))


def main() -> int:
    def p1():
        s = present.summarize(base())
        return s["level"] == "ok" and "当前网络可用" in s["text"] and "ExampleNet" in s["text"]
    case("P1 全部正常 → 当前网络可用", p1)

    def p2():
        s = base(basic=r("故障", "直连探针未通过"))
        out = present.summarize(s)
        return out["text"] == "直连异常，代理通道可用"
    case("P2 直连异常但代理可用", p2)

    def p3():
        s = base(service=r("故障", "端口在监听但健康检查未通过", kind="service_health"))
        out = present.summarize(s)
        return out["level"] == "attention" and "本地服务" in out["text"]
    case("P3 已启用服务故障 → 需要处理", p3)

    def p4():
        s = base(auth=r("不适用", "当前使用其他网络"))
        out = present.summarize(s)
        return out["level"] == "ok"
    case("P4 认证不适用：不降低整体结论", p4)

    def p5():
        s = base()
        s["generated_at"] = NOW - 700
        return present.summarize(s, now=NOW)["level"] == "pending"
    case("P5 过期 → 待确认", p5)

    def p7():
        items = present.pending_items(base(
            auth=r("故障", "认证页不可达", kind="auth"),
            service=r("故障", "端口在监听但健康检查未通过", kind="service_health")))
        by = {i["key"]: i for i in items}
        return (len(items) == 2 and by["auth"]["action_id"] == "open_auth_page"
                and by["service"]["action_id"] == "show_service_card")
    case("P7 待处理映射（认证→打开认证页；服务→看卡片）", p7)

    def p8():
        items = present.pending_items(base(proxy=r("故障", "未知现象")))
        return items[0]["action_id"] == "show_details" and "尚未确认" in items[0]["text"]
    case("P8 未定位原因 → 原因尚未确认", p8)

    def p9():
        okfb = present.operation_feedback(True, "closed", "")
        inc = present.operation_feedback(False, "incomplete", "")
        dec = present.operation_feedback(False, "elevation-declined", "")
        return (okfb["state"] == "已完成" and okfb["verify"] == "连接已复查"
                and inc["steps"][0]["label"] == "重新检查"
                and "UAC" in dec["steps"][0]["label"])
    case("P9 操作反馈：成功带核验；失败给下一步", p9)

    def p10():
        return present.operation_feedback(True, "noop", "")["state"] == "无需处理"
    case("P10 noop → 无需处理", p10)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
