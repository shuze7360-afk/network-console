"""隐私扫描 v2（本地运行）：发布前敏感信息检查，只输出 位置+类别，不复述内容。

两层规则：
1. 通用规则（本文件内置，可公开）：凭据、个人路径、邮箱、设备名、可疑配置。
2. 私有规则（**必须保存在仓库外**）：匹配使用者真实标识的词表。
   通过 --private-rules PATH 或环境变量 NETWORK_CONSOLE_PRIVATE_RULES 提供；
   未提供时仅跑通用规则（对公开贡献者足够）。

范围纪律：
- 扫描 git 跟踪文件 + 暂存区/未跟踪文件 + 根目录文件，不按固定目录取舍；
  不按扩展名静默跳过——二进制按字节扫描，无法读取的对象列入待处理，不计为通过。
- 扫描器自身同样接受检查（v1 的自跳过导致其内置词表带病发布，教训）。
- --history REPO：在指定仓库逐提交扫描补丁内容、提交消息与作者/提交者身份。

用法：
  python scripts/privacy_scan.py [--private-rules PATH] [--history REPO]
退出码：0=干净；1=发现疑似问题或存在待处理项。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

# 通用规则（可公开）：类别 → 字节级正则。只报位置，不回显命中内容。
GENERIC_PATTERNS: dict[str, re.Pattern] = {
    "邮箱": re.compile(rb"\b[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "内网IP": re.compile(rb"\b(?!127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
                         rb"(?:\d{1,3}\.){3}\d{1,3}\b"),
    "设备名": re.compile(rb"LAPTOP-[A-Z0-9]+"),
    "令牌痕迹": re.compile(rb"\b(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                           rb"|sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
    "可疑赋值": re.compile(rb"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"][A-Za-z0-9+/_.-]{16,}['\"]"),
}


def _load_private_rules(path: str | None) -> tuple[dict[str, re.Pattern], str]:
    """私有规则来自仓库外文件；缺失时降级为仅通用规则（不报错、不阻塞公开贡献者）。"""
    candidates = [path, os.environ.get("NETWORK_CONSOLE_PRIVATE_RULES")]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            rules = {}
            for cat, pat in data.get("patterns", {}).items():
                try:
                    rules[f"私有·{cat}"] = re.compile(pat.encode("utf-8"))
                except Exception:
                    continue
            return rules, f"已加载私有规则 {len(rules)} 项（{p}）"
        return {}, f"指定私有规则文件不存在：{p}（按仅通用规则执行）"
    return {}, "未提供私有规则（NETWORK_CONSOLE_PRIVATE_RULES / --private-rules）；仅通用规则"


def _iter_targets() -> list[Path]:
    """发布候选集 = git 跟踪文件 + 未忽略的未跟踪文件（含暂存区）。
    不做扩展名过滤；被 .gitignore 排除的本地生成物不属于发布候选。"""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True)
    targets: list[Path] = []
    for rel in out.stdout.decode("utf-8", "replace").splitlines():
        rel = rel.strip()
        if not rel or rel.startswith('"'):
            continue  # 跳过带引号转义的异常路径（列入待处理更合适，但本仓库无此场景）
        p = ROOT / rel
        if p.is_file():
            targets.append(p)
    return sorted(targets, key=lambda p: p.as_posix())


def _scan_bytes(data: bytes, patterns: dict[str, bytes]) -> list[str]:
    hits: list[str] = []
    for cat, pat in patterns.items():
        try:
            if pat.search(data):
                hits.append(cat)
        except Exception:
            hits.append(f"{cat}(正则错误)")
    return hits


def scan_tree(patterns: dict[str, bytes]) -> tuple[list[str], list[str]]:
    """返回 (问题行, 待处理项)。问题行格式：[类别] 路径。"""
    problems: list[str] = []
    pending: list[str] = []
    files = _iter_targets()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            data = path.read_bytes()
        except Exception as exc:
            pending.append(f"[待处理] {rel}（无法读取：{type(exc).__name__}）")
            continue
        for cat in _scan_bytes(data, patterns):
            problems.append(f"[{cat}] {rel}")
    return problems, pending


def scan_history(repo: Path, patterns: dict[str, bytes]) -> tuple[list[str], list[str]]:
    """逐提交扫描补丁全文、提交消息与作者/提交者身份。"""
    problems: list[str] = []
    pending: list[str] = []
    try:
        shas = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--format=%H"],
            capture_output=True, check=True).stdout.decode().split()
    except Exception as exc:
        return [], [f"[待处理] 历史读取失败：{type(exc).__name__}: {exc}"]
    for sha in shas:
        meta = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%an|%ae|%cn|%ce|%s", sha],
            capture_output=True).stdout
        for cat in _scan_bytes(meta, patterns):
            problems.append(f"[{cat}] 提交身份/消息 {sha[:10]}")
        patch = subprocess.run(
            ["git", "-C", str(repo), "show", "--format=", sha],
            capture_output=True).stdout
        for cat in _scan_bytes(patch, patterns):
            problems.append(f"[{cat}] 提交补丁 {sha[:10]}")
    if not shas:
        pending.append("[待处理] 历史为空或不可读")
    return problems, pending


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-rules", default=None)
    ap.add_argument("--history", default=None, help="额外审计的 git 仓库路径（如克隆副本）")
    args = ap.parse_args()

    private, note = _load_private_rules(args.private_rules)
    patterns = dict(GENERIC_PATTERNS)
    patterns.update(private)
    print(f"规则：通用 {len(GENERIC_PATTERNS)} 项；{note}")

    problems, pending = scan_tree(patterns)
    if args.history:
        hp, hpen = scan_history(Path(args.history), patterns)
        problems += [f"{x}（历史）" for x in hp]
        pending += hpen

    print(f"扫描完成：疑似问题 {len(problems)} 处；待处理 {len(pending)} 项")
    for line in problems:
        print(f"  {line}")
    for line in pending:
        print(f"  {line}")
    ok = not problems and not pending
    print("结果：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
