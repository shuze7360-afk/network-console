"""隐私扫描 v3（本地运行）：发布前敏感信息检查，只输出 位置+类别，不复述内容。

**检查真正将上传的内容**（v3 审核修正）：
- 四个范围分账：工作区文件、暂存区实际 blob、候选提交树（HEAD）、
  历史全部可达对象——不用工作目录内容替代暂存区或提交树。
- Git 枚举一律 NUL 分隔（-z），中文/空格/特殊文件名不被跳过、不被引号截断；
  所有 git 命令检查退出状态；对象缺失、读取失败、未合并索引（冲突）、
  枚举失败一律列入待处理并使退出码非零，绝不计为通过。
- 历史命中按「对象编号＋类别」去重，保留引用路径与提交定位；补丁重复
  出现不计为新的泄露对象。
- 两层规则：通用规则内置（可公开）；私有规则**保存在仓库外**。显式指定
  私有规则但文件不存在/不可读/格式错误/空规则集/正则无效 → 阻断（非零退出）；
  未指定规则 → 仅跑通用规则，并在报告中标注扫描范围（对公开贡献者足够）。
- 扫描器自身同样在被扫描集合内。

用法：
  python scripts/privacy_scan.py [--private-rules PATH]
退出码：0=干净；1=发现疑似问题或存在待处理项；2=私有规则阻断。
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


class GitError(Exception):
    """git 命令失败（非零退出）。调用方必须转为待处理项，不得静默跳过。"""


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} 退出码 {proc.returncode}: "
                       + proc.stderr.decode("utf-8", "replace").strip()[:160])
    return proc.stdout


def _load_private_rules(path: str | None) -> tuple[dict[str, re.Pattern], str, int]:
    """显式指定的私有规则必须可用：文件不存在/不可读/格式错误/空规则集/正则
    无效都抛 RulesError（阻断）。未指定 → 仅通用规则并在报告标注范围。"""
    if not path and not os.environ.get("NETWORK_CONSOLE_PRIVATE_RULES"):
        return {}, "未提供私有规则（--private-rules / NETWORK_CONSOLE_PRIVATE_RULES）；本次仅覆盖通用规则", 0
    p = Path(path or os.environ["NETWORK_CONSOLE_PRIVATE_RULES"])
    if not p.exists():
        raise RulesError(f"私有规则文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RulesError(f"私有规则文件不可读或非 JSON：{p}（{type(exc).__name__}）") from exc
    if not isinstance(data, dict) or not isinstance(data.get("patterns"), dict) or not data["patterns"]:
        raise RulesError(f"私有规则文件为空或格式错误（需要 {{\"patterns\": {{...}}}}）：{p}")
    rules: dict[str, re.Pattern] = {}
    for cat, pat in data["patterns"].items():
        try:
            rules[f"私有·{cat}"] = re.compile(str(pat).encode("utf-8"))
        except Exception as exc:
            raise RulesError(f"私有规则「{cat}」正则无效：{exc}") from exc
    if not rules:
        raise RulesError(f"私有规则文件没有可用规则：{p}")
    return rules, f"已加载私有规则 {len(rules)} 项（{p}）", len(rules)


class RulesError(Exception):
    """私有规则显式指定但不可用 → 阻断检查。"""


def _scan_bytes(data: bytes, patterns: dict[str, re.Pattern]) -> list[str]:
    hits: list[str] = []
    for cat, pat in patterns.items():
        try:
            if pat.search(data):
                hits.append(cat)
        except Exception as exc:
            hits.append(f"{cat}(扫描器异常:{type(exc).__name__})")
    return hits


def _entry_names(repo: Path) -> list[str]:
    """跟踪 + 未忽略未跟踪文件的 NUL 分隔清单（工作区扫描范围）。"""
    out = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n]


def _index_blobs(repo: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """暂存区 blob 清单（NUL 分隔 --stage）。返回 ([(名称, sha)], 待处理)。"""
    pending: list[str] = []
    out = _git(repo, "ls-files", "-z", "--cached", "--stage")
    entries: list[tuple[str, str]] = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        try:
            meta, name_b = rec.split(b"\t", 1)
            _mode, sha, stage = meta.decode("ascii", "replace").split()
            name = name_b.decode("utf-8", "surrogateescape")
            if stage != "0":
                pending.append(f"[待处理] 暂存区存在未合并条目（stage={stage}）：{name}")
                continue
            entries.append((name, sha))
        except Exception as exc:
            pending.append(f"[待处理] 暂存区条目解析失败：{type(exc).__name__}")
    return entries, pending


def _tree_blobs(repo: Path, treeish: str) -> tuple[list[tuple[str, str]], list[str]]:
    """提交树内全部 blob（NUL 分隔 ls-tree -r）。"""
    out = _git(repo, "ls-tree", "-r", "-z", treeish)
    entries: list[tuple[str, str]] = []
    pending: list[str] = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        try:
            meta, name_b = rec.split(b"\t", 1)
            mode, otype, sha = meta.decode("ascii", "replace").split()
            if otype != "blob":
                continue
            entries.append((name_b.decode("utf-8", "surrogateescape"), sha))
        except Exception as exc:
            pending.append(f"[待处理] 树条目解析失败：{type(exc).__name__}")
    return entries, pending


def _blob(repo: Path, sha: str, pending: list[str]) -> bytes | None:
    try:
        return _git(repo, "cat-file", "blob", sha)
    except GitError as exc:
        pending.append(f"[待处理] 对象读取失败 {sha[:12]}：{exc}")
        return None


def scan_index(repo: Path, patterns: dict[str, re.Pattern]) -> tuple[list[str], list[str]]:
    """暂存区实际 blob（不是工作目录内容的替身）。"""
    problems: list[str] = []
    pending: list[str] = []
    entries, pend = _index_blobs(repo)
    pending += pend
    seen: set[str] = set()
    for name, sha in entries:
        if sha in seen:
            continue
        seen.add(sha)
        data = _blob(repo, sha, pending)
        if data is None:
            continue
        for cat in _scan_bytes(data, patterns):
            problems.append(f"[{cat}] 暂存区 {name}（blob {sha[:12]}）")
    return problems, pending


def scan_tree(repo: Path, treeish: str, patterns: dict[str, re.Pattern],
              label: str) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    pending: list[str] = []
    entries, pend = _tree_blobs(repo, treeish)
    pending += pend
    seen: set[str] = set()
    for name, sha in entries:
        if sha in seen:
            continue
        seen.add(sha)
        data = _blob(repo, sha, pending)
        if data is None:
            continue
        for cat in _scan_bytes(data, patterns):
            problems.append(f"[{cat}] {label} {name}（blob {sha[:12]}）")
    return problems, pending


def scan_history(repo: Path, patterns: dict[str, re.Pattern]) -> tuple[list[str], list[str], dict]:
    """全部已取得引用可达的提交：提交对象（消息正文+身份元数据）与全部文件
    blob（含二进制、合并提交引入的内容、非当前分支）。命中按（对象编号＋类别）
    去重，保留引用路径与提交定位。"""
    problems: list[str] = []
    pending: list[str] = []
    refs = _git(repo, "show-ref", "--head").decode("utf-8", "replace").splitlines()
    commits = _git(repo, "rev-list", "--all").decode().split()
    blob_paths: dict[str, set[str]] = {}
    blob_commits: dict[str, set[str]] = {}
    object_hits: dict[tuple[str, str], None] = {}
    for csha in commits:
        # 提交对象本体：作者/提交者身份 + 消息正文
        try:
            commit_obj = _git(repo, "cat-file", "commit", csha)
        except GitError as exc:
            pending.append(f"[待处理] 提交对象读取失败 {csha[:12]}：{exc}")
            continue
        for cat in _scan_bytes(commit_obj, patterns):
            key = (csha, cat)
            object_hits.setdefault(key, None)
            problems.append(f"[{cat}] 历史提交对象 {csha[:12]}（身份/消息）")
        # 该提交树内的 blob
        try:
            entries, pend = _tree_blobs(repo, csha)
        except GitError as exc:
            pending.append(f"[待处理] 提交树读取失败 {csha[:12]}：{exc}")
            continue
        pending += pend
        for name, sha in entries:
            blob_paths.setdefault(sha, set()).add(name)
            blob_commits.setdefault(sha, set()).add(csha)
    # blob 对象只扫一次（含二进制），命中按（对象，类别）去重
    for sha, paths in sorted(blob_paths.items()):
        data = _blob(repo, sha, pending)
        if data is None:
            continue
        for cat in _scan_bytes(data, patterns):
            key = (sha, cat)
            if key in object_hits:
                continue
            object_hits[key] = None
            problems.append(f"[{cat}] 历史对象 {sha[:12]} 路径 {sorted(paths)[0]}"
                            f"（被 {len(blob_commits[sha])} 个提交引用）")
    stats = {"refs": len(refs), "commits": len(commits),
             "blobs": len(blob_paths), "object_hits": len(object_hits)}
    return problems, pending, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-rules", default=None)
    args = ap.parse_args()

    try:
        private, note, n_rules = _load_private_rules(args.private_rules)
    except RulesError as exc:
        print(f"私有规则阻断：{exc}")
        print("结果： FAIL（阻断）")
        return 2
    patterns = dict(GENERIC_PATTERNS)
    patterns.update(private)

    pending: list[str] = []
    work_problems: list[str] = []
    index_problems: list[str] = []
    tree_problems: list[str] = []
    hist_problems: list[str] = []
    stats: dict = {}
    files: list[str] = []
    try:
        files = _entry_names(ROOT)
        for rel in files:
            p = ROOT / rel
            try:
                data = p.read_bytes()
            except Exception as exc:
                pending.append(f"[待处理] 工作区文件无法读取 {rel}（{type(exc).__name__}）")
                continue
            for cat in _scan_bytes(data, patterns):
                work_problems.append(f"[{cat}] 工作区 {rel}")
        index_problems, pend = scan_index(ROOT, patterns)
        pending += pend
        head = _git(ROOT, "rev-parse", "HEAD").decode().strip()
        tree_problems, pend = scan_tree(ROOT, head, patterns, "候选提交树")
        pending += pend
        hist_problems, pend, stats = scan_history(ROOT, patterns)
        pending += pend
    except GitError as exc:
        pending.append(f"[待处理] git 枚举失败：{exc}")

    problems = work_problems + index_problems + tree_problems + hist_problems
    print(f"规则：通用 {len(GENERIC_PATTERNS)} 项；{note}")
    print(f"范围：工作区文件 {len(files)}；暂存 blob 已核；"
          f"候选树(HEAD) 已核；历史可达提交 {stats.get('commits', 0)} 个、"
          f"引用 {stats.get('refs', 0)} 条、唯一 blob {stats.get('blobs', 0)} 个")
    print(f"[工作区] 命中 {len(work_problems)} 处；[暂存区] {len(index_problems)} 处；"
          f"[候选提交树] {len(tree_problems)} 处；[历史对象去重] {len(hist_problems)} 处"
          f"（对象×类别；历史唯一对象 {stats.get('blobs', 0)} 个）")
    for line in problems + pending:
        print(f"  {line}")
    ok = not problems and not pending
    print("结果：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
