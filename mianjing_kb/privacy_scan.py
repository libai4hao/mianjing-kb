# -*- coding: utf-8 -*-
"""privacy_scan：提交前扫描仓库里的个人信息 / 本机路径 / 密钥残留。

扫描对象是 **git 已跟踪文件**（默认）或 **暂存区**（--staged，供 pre-commit 调用），
不扫未跟踪文件——工作区的临时产物不该挡住提交。
`--staged` 读的是 index 里的 blob（`git show :path`），不是工作区当前内容：
先写好、再"临时改回"的文件仍按将要提交的内容判定。
覆盖边界：只看**当前内容**；历史提交里的泄露需另用 gitleaks / trufflehog 扫 git log。

规则（命中即失败）：
  abs_path      任意 Windows 绝对路径（盘符 + 根，如 C:\\Users\\x、D:\\corpus），
                仓库文档只允许 `<CORPUS_ROOT>` 这类占位符；合成测试数据用行内 allow 豁免
  home_path     POSIX 家目录 / /home/<name>、/Users/<name>
  personal_mail 非 GitHub noreply 的个人邮箱（QQ/163/126/Gmail/Outlook/Foxmail…）
  cn_mobile     中国大陆手机号
  secret        sk-… / AKIA… / api_key=… 形态的字面量密钥
  corpus_meta   真实语料清单（meta/skip_log.json 等含来源文件名的文件）被跟踪

用法：
    python -m mianjing_kb.privacy_scan             # 扫已跟踪文件
    python -m mianjing_kb.privacy_scan --staged    # 只扫暂存区（pre-commit）
    python -m mianjing_kb.privacy_scan --json      # 机器可读
退出码：0 干净 / 1 有命中 / 2 不是 git 仓库或 git 不可用。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TEXT_EXT = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".sql", ".ps1", ".bat",
            ".cfg", ".ini", ".html", ".js", ".ts", ".css", ".gitignore", ""}

RULES = [
    # 任意 Windows 绝对路径：盘符 + 根分隔符，不限定第二级目录名。
    # 旧规则只认 Users/dev/utools/my-resource/Project/Infra 白名单前缀，
    # D:\corpus\x、E:\Data\y 这种同样泄露本机布局的路径会漏检。
    # 负向前瞻挡掉 URL（https:// 的 "s:/" 前一个字符是字母）。
    ("abs_path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
    # POSIX 家目录（Windows 侧已由 abs_path 覆盖）
    ("home_path", re.compile(r"/(?:home|Users)/[^\s/]+")),
    ("personal_mail", re.compile(r"[\w.+-]+@(?:qq|163|126|gmail|outlook|foxmail|sina|hotmail)\.com",
                                 re.IGNORECASE)),
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("secret", re.compile(r"(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|api[_-]?key\s*[=:]\s*"
                          r"[\"'][^\"'\s]{12,}[\"'])", re.IGNORECASE)),
]
BLOCKED_FILES = {"meta/skip_log.json"}
# 自身含有规则字面量（正则与说明里必然出现本机路径样式），扫描时跳过自己，避免自我误报
SELF = "mianjing_kb/privacy_scan.py"


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=REPO,
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "git 失败")
    return out.stdout


def list_files(staged: bool) -> list[str]:
    if staged:
        return [f for f in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if f]
    return [f for f in _git("ls-files").splitlines() if f]


def _staged_text(rel: str) -> str | None:
    """读暂存区 blob（而不是工作区文件）：改完又改回去也必须按将要提交的内容判。"""
    try:
        return _git("show", f":{rel}")
    except RuntimeError:
        return None


def scan(files: list[str], *, staged: bool = False, stats: dict | None = None) -> list[dict]:
    """扫给定文件；stats（可选）收集跳过原因，避免"扫了 0 个也算干净"。"""
    hits = []
    skipped = stats.setdefault("skipped", []) if stats is not None else None

    def _skip(rel: str, why: str) -> None:
        if skipped is not None:
            skipped.append({"file": rel, "why": why})

    for rel in files:
        p = REPO / rel
        if rel in BLOCKED_FILES:
            hits.append({"file": rel, "line": 0, "rule": "corpus_meta", "text": rel})
            continue
        if not p.is_file():
            continue                       # 删除/改名：暂存区没有新内容
        if rel == SELF:
            _skip(rel, "规则字面量自我豁免")
            continue
        if p.suffix.lower() not in TEXT_EXT:
            _skip(rel, "非文本扩展名")
            continue
        if p.stat().st_size > 512 * 1024:
            _skip(rel, f"超过 512KB（{p.stat().st_size}）")
            continue
        if staged:
            text = _staged_text(rel)
            if text is None:
                _skip(rel, "暂存区读不到内容")
                continue
        else:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as ex:
                _skip(rel, f"读取失败: {ex}")
                continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "privacy-scan: allow" in line:      # 合成测试数据可显式豁免（逐行标注，不整文件放行）
                continue
            for rule, rx in RULES:
                m = rx.search(line)
                if m:
                    hits.append({"file": rel, "line": lineno, "rule": rule,
                                 "text": _mask(m.group(0))})
                    break
    return hits


def _mask(s: str) -> str:
    return s[:4] + "***" if len(s) > 4 else "***"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="隐私 / 密钥门禁")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区（pre-commit 用）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)
    try:
        files = list_files(args.staged)
    except (RuntimeError, FileNotFoundError) as ex:
        print(f"[error] 无法读取 git 文件列表：{ex}", file=sys.stderr)
        return 2
    hits = scan(files, staged=args.staged, stats=(stats := {}))
    skipped = stats.get("skipped") or []
    if args.json:
        print(json.dumps({"scanned": len(files), "hits": hits, "skipped": skipped},
                         ensure_ascii=False, indent=1))
    else:
        print(f"[scan] {len(files)} 个文件（跳过 {len(skipped)}），命中 {len(hits)} 处")
        for s in skipped:
            print(f"  [skip] {s['file']}  {s['why']}")
        for h in hits:
            print(f"  {h['file']}:{h['line']}  [{h['rule']}]  {h['text']}")
        if hits:
            print("\n[block] 请改成占位符（如 <CORPUS_ROOT>）或把个人信息移出仓库后再提交",
                  file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
