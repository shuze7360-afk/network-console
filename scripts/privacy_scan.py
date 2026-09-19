"""隐私扫描（本地运行）：扫描待发布文件中的敏感信息，只输出 位置+类别。

用法：python scripts/privacy_scan.py
退出码：0=干净；1=发现疑似问题。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["netconsole", "scripts", "docs", "reports"]
SCAN_EXT = {".py", ".md", ".json", ".txt", ".bat", ".ps1", ".xml", ".html", ".cfg"}

# 类别 → 正则（只报位置，不回显命中内容）
PATTERNS = {
    "邮箱": re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "内网IP": re.compile(r"\b(?!127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
                         r"(?:\d{1,3}\.){2}\d{1,3}\.\d{1,3}\b"),
    "设备名": re.compile(r"LAPTOP-[A-Z0-9]+", re.I),
    "令牌痕迹": re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|gho_[A-Za-z0-9]{20,})\b"),
}


def main() -> int:
    hits: list[tuple[str, int, str]] = []
    files = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in SCAN_EXT or not path.is_file():
                continue
            if path.name == "privacy_scan.py":
                continue  # 扫描器自身包含模式清单，跳过自匹配
            files += 1
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for cat, pat in PATTERNS.items():
                for m in pat.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    hits.append((f"{path.relative_to(ROOT)}:{line}", cat, ""))
    print(f"扫描文件 {files} 个；疑似问题 {len(hits)} 处")
    for loc, cat, _ in hits:
        print(f"  [{cat}] {loc}")
    print("结果：", "PASS" if not hits else "FAIL")
    return 0 if not hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
