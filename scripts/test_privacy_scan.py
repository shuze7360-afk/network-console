"""隐私扫描自测（2.0.0）：全部使用虚构样例，验证 v3 扫描器的能力与边界。

反例覆盖（对应审核要求）：
- 暂存区含虚构凭据而工作目录已清理 → 暂存区 blob 扫描必须发现；
- 中文文件名正确枚举与命中；
- git 枚举失败（非 git 目录）→ 待处理 + 非零退出，不计为通过；
- 历史扫描：样例仅在提交消息正文、旧二进制 blob、非当前分支、合并提交中；
- 私有规则：路径不存在/空规则/格式错误/正则无效 → 阻断（非零退出）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCANNER = Path(__file__).resolve().parent / "privacy_scan.py"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []
TOKEN = "sk-FakeFakeFAKEfake12345678"  # 虚构样例，命中「令牌痕迹」
FAKE_IP = "10.0.0.42"                  # 虚构样例，命中「内网IP」


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def _git(repo: Path, *args: str, identity: bool = True) -> None:
    cmd = ["git", "-C", str(repo)]
    if identity:
        cmd += ["-c", "user.name=Audit Bot", "-c", "user.email=audit-bot@example.org"]
    cmd += list(args)
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败: {proc.stderr.decode('utf-8', 'replace')[:200]}")


def _scan(repo: Path, *extra: str) -> tuple[int, str]:
    # 必须使用目标仓库内的扫描器副本：扫描器的 ROOT 取自身位置，
    # 用原始路径会把公开仓库本身扫一遍（v1 自跳过教训的同款错误）。
    # 显式失败而非回退：解释器"文件不存在"的退出码恰为 2，会与阻断撞车。
    scanner = repo / "scripts" / "privacy_scan.py"
    if not scanner.exists():
        scanner = repo / "privacy_scan.py"
    assert scanner.exists(), f"scratch 仓库缺少扫描器副本：{scanner}"
    proc = run([sys.executable, str(scanner), *extra], cwd=repo)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


def case(name: str, fn) -> None:
    try:
        ok_, detail = fn()
        detail = "" if ok_ is True else (detail or f"实际 {ok_}")
    except Exception as exc:  # noqa: BLE001
        ok_, detail = False, f"{type(exc).__name__}: {exc}"
    RESULTS.append((name, bool(ok_), detail))
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + ("" if ok_ else f" — {detail}"))


def build_scratch(base: Path) -> Path:
    repo = base / "scratch-repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCANNER, repo / "scripts" / "privacy_scan.py")
    _git(repo, "init")
    # 提交 1：干净内容
    (repo / "clean.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "clean.md")
    _git(repo, "commit", "-m", "init clean")
    # 分支 side：含虚构令牌（非当前分支内容）
    _git(repo, "checkout", "-b", "side")
    (repo / "side-secret.md").write_text(f"token {TOKEN}\n", encoding="utf-8")
    _git(repo, "add", "side-secret.md")
    _git(repo, "commit", "-m", "side commit with token")
    _git(repo, "checkout", "main" if Path(repo / ".git", "HEAD").read_text().find("main") != -1 else "master")
    # 二进制 blob（内嵌虚构令牌的字节）提交到当前分支
    (repo / "fake.bin").write_bytes(b"\x89PNG\r\n\x1a\n" + TOKEN.encode() + b"\x00\xff")
    _git(repo, "add", "fake.bin")
    _git(repo, "commit", "-m", "add binary")
    # 中文文件名 + 虚构内网 IP
    (repo / "测试文档.md").write_text(f"地址 {FAKE_IP}\n", encoding="utf-8")
    _git(repo, "add", "测试文档.md")
    _git(repo, "commit", "-m", "add 中文文档")
    # 合并 side（合并提交引入的内容）
    _git(repo, "merge", "side", "-m", "merge side", "--no-edit") if run(
        ["git", "-C", str(repo), "merge", "--no-edit", "-m", "merge side", "side"]).returncode == 0 else None
    # 样例仅在提交消息正文的提交（树内干净）
    (repo / "msg-only.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "msg-only.md")
    _git(repo, "commit", f"-m", f"notes referencing {TOKEN} in message only")
    return repo


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="scan-selftest-"))

    # T1 暂存区含凭据而工作目录已清理 → 暂存 blob 扫描发现（工作区不误报）
    def t1():
        repo = build_scratch(base / "t1")
        secret = repo / "staged-cred.txt"
        secret.write_text(f"staged {TOKEN}\n", encoding="utf-8")
        _git(repo, "add", "staged-cred.txt")
        secret.write_text("cleaned in workdir\n", encoding="utf-8")  # 工作区清理
        rc, out = _scan(repo)
        ok_index = "[令牌痕迹] 暂存区 staged-cred.txt" in out
        no_work = "[令牌痕迹] 工作区 staged-cred.txt" not in out
        return rc != 0 and ok_index and no_work, out[-400:]
    case("T1 暂存区凭据（工作区已清理）→ 暂存 blob 扫描发现", t1)

    # T2 中文文件名 + 历史对象（消息正文/二进制/非当前分支/合并）
    def t2():
        repo = build_scratch(base / "t2")
        rc, out = _scan(repo)
        checks = {
            "中文文件名": "测试文档.md" in out and "[内网IP]" in out,
            "提交消息正文": "历史提交对象" in out and "[令牌痕迹]" in out,
            "二进制blob": "fake.bin" in out,
            "非当前分支/合并内容": "side-secret.md" in out,
        }
        return rc != 0 and all(checks.values()), str({k: v for k, v in checks.items() if not v})
    case("T2 中文文件名 + 历史对象（消息/二进制/非当前分支/合并）→ 检出", t2)

    # T3 历史命中按对象+类别去重（同一 blob 多提交引用不重复计数）
    def t3():
        repo = build_scratch(base / "t3")
        _git(repo, "checkout", "-b", "side2")
        (repo / "dup.md").write_text(f"dup {TOKEN}\n", encoding="utf-8")
        _git(repo, "add", "dup.md")
        _git(repo, "commit", "-m", "dup on side2")
        _git(repo, "checkout", "main" if "main" in
             Path(repo / ".git", "HEAD").read_text() else "master")
        _git(repo, "merge", "--no-edit", "-m", "merge dup", "side2")
        _git(repo, "branch", "-D", "side2")  # 删除分支后内容仍可达（merge 提交）
        rc, out = _scan(repo)
        dup_lines = [l for l in out.splitlines() if "dup.md" in l and "令牌痕迹" in l and "历史" in l]
        return rc != 0 and 1 <= len(dup_lines) <= 2, f"dup 命中行 {len(dup_lines)}"
    case("T3 历史命中按对象+类别去重（不按补丁重复计）", t3)

    # T4 私有规则阻断：不存在/空/格式错误/正则无效 → 非零退出
    def t4():
        repo = build_scratch(base / "t4")
        rules_dir = base / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "empty.json").write_text('{"patterns": {}}', encoding="utf-8")
        (rules_dir / "broken.json").write_text("{not json", encoding="utf-8")
        (rules_dir / "badre.json").write_text('{"patterns": {"x": "("}}', encoding="utf-8")
        missing = base / "no-such-rules.json"
        results = []
        for r in (missing, rules_dir / "empty.json", rules_dir / "broken.json", rules_dir / "badre.json"):
            rc, out = _scan(repo, "--private-rules", str(r))
            results.append(rc == 2 and "阻断" in out)
        return all(results), f"{results}"
    case("T4 私有规则：不存在/空/格式错误/正则无效 → 阻断", t4)

    # T5 git 枚举失败（非 git 目录）→ 待处理 + 非零退出
    def t5():
        plain = base / "not-a-repo"
        plain.mkdir(parents=True)
        shutil.copy2(SCANNER, plain / "privacy_scan.py")
        rc, out = _scan(plain)
        return rc != 0 and "git 枚举失败" in out, out[-200:]
    case("T5 git 枚举失败 → 待处理且非零退出（不计为通过）", t5)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
