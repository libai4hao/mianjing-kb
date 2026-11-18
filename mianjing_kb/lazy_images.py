# -*- coding: utf-8 -*-
"""P2-⑥ png 懒加载：20-题库MD 全部 markdown 图片引用改写为 <img loading="lazy">。

动机：16,263 处 `![](./images/xxx.png)` 内联引用，编辑器/预览全量加载图片拖慢打开。
改写：`![alt](path)` → `<img src="path" alt="alt" loading="lazy" />`（Obsidian 预览与浏览器均支持）。
跳过：路径含 `)`（正则无法安全解析，如 `.0(暗黑版)` 类脏名）、非图片后缀、路径/alt 含 `"`。
备份：旧 md 全量备份 build/lazyimg_backup/（rel 平铺改名），可整目录回滚。
幂等：已是 <img 的行不动；无可转换引用的文件跳过。

用法：
    python -m mianjing_kb.lazy_images --dry-run    # 只统计
    python -m mianjing_kb.lazy_images              # 实际改写
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config  # noqa: E402

MD_ROOT = Path(config.CORPUS_ROOT) / "20-题库MD"
BACKUP = Path(config.BUILD_DIR) / "lazyimg_backup"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(((?:[^()]|\([^()]*\))+)\)")  # 路径允许一层嵌套括号
IMG_EXT = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"}


def convert_text(text: str) -> tuple[str, int, int, list[str]]:
    """返回 (新文本, 转换数, 跳过数, 跳过样例)。"""
    skipped, samples = 0, []

    def repl(m: re.Match) -> str:
        nonlocal skipped
        alt, path = m.group(1), m.group(2)
        if Path(path).suffix.lower() not in IMG_EXT or '"' in alt or '"' in path:
            skipped += 1
            if len(samples) < 5:
                samples.append(m.group(0)[:80])
            return m.group(0)
        return f'<img src="{path}" alt="{alt}" loading="lazy" />'

    new, n = IMG_RE.subn(repl, text)
    return new, n, skipped, samples


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="md 图片引用懒加载改写")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    started = time.perf_counter()
    files_changed = refs_ok = refs_skip = 0
    skip_samples: list[str] = []
    for p in sorted(MD_ROOT.rglob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "<img " in text:  # 已处理过的文件不动（幂等）
            continue
        new, n, sk, samples = convert_text(text)
        refs_skip += sk
        if samples and len(skip_samples) < 5:
            skip_samples += samples
        if n == 0:
            continue
        refs_ok += n
        files_changed += 1
        if not args.dry_run:
            BACKUP.mkdir(parents=True, exist_ok=True)
            bak = BACKUP / p.relative_to(MD_ROOT).as_posix().replace("/", "_")
            if not bak.exists():
                shutil.copy2(p, bak)
            p.write_text(new, encoding="utf-8")
    stats = {"dry_run": args.dry_run, "files_changed": files_changed,
             "refs_converted": refs_ok, "refs_skipped": refs_skip,
             "skip_samples": skip_samples,
             "elapsed_sec": round(time.perf_counter() - started, 1),
             "backup": str(BACKUP) if not args.dry_run else None}
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
