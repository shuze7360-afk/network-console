"""视觉场景截图与验证（隔离模式：不触网、不起服务、不动持久化状态）。

全部样例为虚构数据；覆盖 明/暗 × 正常、全部关闭、混合故障、认证不适用、
超长内容；可加 --scale 2 复测高缩放。产物：reports/visual/。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT = Path(__file__).resolve().parents[1] / "reports" / "visual"
NOW = time.time()


def r(status: str, reason: str = "", ms: int = 100, kind: str | None = None,
      evidence: dict | None = None) -> dict:
    return {"status": status, "reason": reason, "checked_at": NOW, "probe_ms": ms,
            "evidence": evidence or {}, "kind": kind}


def links(**kw) -> dict:
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


SAMPLES = {
    "normal": (
        links(),
        [{"level": "info", "code": "ENV_PROXY_PRESENT",
          "text": "存在用户级代理环境变量（影响新启动的命令行工具）。"}],
    ),
    "all_closed": (
        links(auth=r("主动关闭", "网络认证功能未启用"),
              proxy=r("主动关闭", "代理连接功能未启用"),
              service=r("主动关闭", "本地服务功能未启用")),
        [],
    ),
    "mixed": (
        links(auth=r("故障", "认证页不可达（连接超时）——可能需要登录", kind="auth"),
              proxy=r("外部已启动", "代理端口在监听，但非控制台启动（轻量核对）"),
              service=r("开启中", "正在启动服务…")),
        [{"level": "warn", "code": "PROXY_BYPASS_MISSING",
          "text": "系统代理已开启但直连例外列表为空。"}],
    ),
    "auth_na": (
        links(auth=r("不适用", "当前使用其他网络（HomeWiFi-5G），跳过认证探测",
                     evidence={"identity": {"state": "other", "ssid": "HomeWiFi-5G"}})),
        [],
    ),
    "long_content": (
        links(proxy=r("故障",
                      "代理端口在监听但转发失败：ConnectTimeoutError —— 目标 example.invalid:8080 "
                      "在 10060ms 内未响应（A connection attempt failed because the connected party "
                      "did not properly respond after a period of time），已重试 3 次，探针 "
                      "http://www.msftconnecttest.com/connecttest.txt",
                      ms=10086),
              service=r("故障", "端口在监听但健康检查未通过（HTTP 500），详见详情",
                        kind="service_health")),
        [{"level": "warn", "code": "PROXY_BYPASS_MISSING", "text": "系统代理已开启但直连例外列表为空。"}],
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", default="")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    if args.scale:
        os.environ["QT_SCALE_FACTOR"] = args.scale

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from netconsole.ui.main_window import MainWindow, make_icon
    from netconsole.ui import theme

    app = QApplication(sys.argv)
    app.setFont(QFont(theme.FONT_FAMILY, 10))

    OUT.mkdir(parents=True, exist_ok=True)
    suffix = f"-x{args.scale}" if args.scale else ""
    only = args.only or None
    made: list[tuple[str, str]] = []

    for name, (sample_links, sample_findings) in SAMPLES.items():
        if only and name != only:
            continue
        snap = {**sample_links, "findings": sample_findings,
                "generated_at": NOW, "host": "example-host"}
        for dark in (False, True):
            theme.theme_manager._set("dark" if dark else "light")
            app.setStyleSheet(theme.theme_manager.stylesheet())  # 渲染前强制套用目标主题
            win = MainWindow(isolated=True)
            win.resize(960, 700)
            win.show()
            app.processEvents()
            win.apply_snapshot(snap)
            if name == "long_content":
                card = win.cards["proxy"]
                if not card.details.isVisible():
                    card._toggle_details()
                if not win.findings_box.isVisible():
                    win._toggle_findings()
            app.processEvents()
            tag = ("dark" if dark else "light") + suffix
            out = OUT / f"ui-{name}-{tag}.png"
            win.grab().save(str(out))
            made.append((f"{name}/{tag}", str(out)))
            win.hide()
            win.deleteLater()
            app.processEvents()

    report = ["# 视觉场景截图（虚构数据，隔离渲染）", "",
              f"缩放：{args.scale or '100%'}", ""]
    root = str(OUT.parent)
    for tag, path in made:
        rel = str(Path(path).resolve().relative_to(Path(root).resolve()))
        report.append(f"- {tag}: `reports/visual/{Path(path).name}`")
    (OUT / f"ui-visual-report{suffix}.md").write_text("\n".join(report), encoding="utf-8")
    print(f"生成 {len(made)} 张截图 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
