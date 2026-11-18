# -*- coding: utf-8 -*-
"""privacy_scan：提交前扫描仓库里的个人信息 / 本机路径 / 密钥残留。

扫描对象是 **git 已跟踪文件**（默认）或 **暂存区**（--staged，供 pre-commit 调用），
不扫未跟踪文件——工作区的临时产物不该挡住提交。
覆盖边界：只看**当前文件内容**；历史提交里的泄露需另用 gitleaks / trufflehog 扫 git log。

规则（命中即失败）：
  abs_path      Windows 绝对路径（C:\\ / D:\\），仓库文档只允许 `<CORPUS_ROOT>` 这类占位符
  home_path     C:\\Users\\<name> / /home/<name>
  personal_mail 非 GitHub noreply 的个人邮箱（QQ/163/126/Gmail/Outlook/Foxmail…）
  cn_mobile     中国大陆手机号
  secret        sk-… / AKIA… / api_key=… 形态的字面量密钥
  corpus_meta   真实语料清单（meta/skip_log.json 等含来源文件名的文件）被跟踪

用法：
    python tools/privacy_scan.py             # 扫已跟踪文件
    python tools/privacy_scan.py --staged    # 只扫暂存区（pre-commit）
    python tools/privacy_scan.py --json      # 机器可读
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
    ("abs_path", re.compile(r"[A-Za-z]:[\\/](?:Users|dev|utools|my-resource|Project|Infra)[\\/]")),
    ("home_path", re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/]|/home/)[^\s\\/]+")),
    ("personal_mail", re.compile(r"[\w.+-]+@(?:qq|163|126|gmail|outlook|foxmail|sina|hotmail)\.com",
                                 re.IGNORECASE)),
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("secret", re.compile(r"(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|api[_-]?key\s*[=:]\s*"
                          r"[\"'][^\"'\s]{12,}[\"'])", re.IGNORECASE)),
]
BLOCKED_FILES = {"meta/skip_log.json"}
# 自身含有规则字面量（正则与说明里必然出现本机路径样式），扫描时跳过自己，避免自我误报
SELF = "tools/privacy_scan.py"


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


def scan(files: list[str]) -> list[dict]:
    hits = []
    for rel in files:
        p = REPO / rel
        if rel in BLOCKED_FILES:
            hits.append({"file": rel, "line": 0, "rule": "corpus_meta", "text": rel})
            continue
        if not p.is_file() or rel == SELF or p.suffix.lower() not in TEXT_EXT \
                or p.stat().st_size > 512 * 1024:
            continue
        p = REPO / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
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
    hits = scan(files)
    if args.json:
        print(json.dumps({"scanned": len(files), "hits": hits}, ensure_ascii=False, indent=1))
    else:
        print(f"[scan] {len(files)} 个文件，命中 {len(hits)} 处")
        for h in hits:
            print(f"  {h['file']}:{h['line']}  [{h['rule']}]  {h['text']}")
        if hits:
            print("\n[block] 请改成占位符（如 <CORPUS_ROOT>）或把个人信息移出仓库后再提交",
                  file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
