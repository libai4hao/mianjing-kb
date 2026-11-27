# -*- coding: utf-8 -*-
"""P2-⑥ png 懒加载：20-题库MD 全部 markdown 图片引用改写为 <img loading="lazy">。

动机：16,263 处 `![](./images/xxx.png)` 内联引用，编辑器/预览全量加载图片拖慢打开。
改写：`![alt](path)` → `<img src="path" alt="alt" loading="lazy" />`（Obsidian 预览与浏览器均支持）。
跳过：路径含 `)`（正则无法安全解析，如 `.0(暗黑版)` 类脏名）、非图片后缀、路径/alt 含 `"`。
备份：旧 md 全量备份 build/lazyimg_backup/（rel+内容双哈希命名，与 ocr_merge 同口径，
可整目录回滚）；备份内容校验通过才改写（审计候选 #31）。
路径安全：walk_files 剪 junction/软链（#32）；原子替换而非原地写。
幂等：已是 <img 的行不动；无可转换引用的文件跳过。

用法：
    python -m mianjing_kb.lazy_images --dry-run    # 只统计
    python -m mianjing_kb.lazy_images              # 实际改写
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import atomic, config, paths  # noqa: E402

MD_ROOT = Path(config.CORPUS_ROOT) / "20-题库MD"
BACKUP = Path(config.BUILD_DIR) / "lazyimg_backup"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(((?:[^()]|\([^()]*\))+)\)")  # 路径允许一层嵌套括号
IMG_EXT = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"}


def backup_name(rel: str, old_bytes: bytes) -> str:
    """rel + 原文内容双哈希（与 ocr_merge.backup_name 同口径）。

    候选 #31：旧实现把 rel 分隔符平铺成下划线，a/b.md 与 a_b.md 撞名后
    后来者的备份被跳过、原文丢失；双哈希键同时消除撞名与内容漂移。
    """
    return (hashlib.sha256(rel.replace("\\", "/").encode("utf-8")).hexdigest()[:16] + "-"
            + hashlib.sha256(old_bytes).hexdigest()[:16] + ".md")


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
    backup_failed: list[str] = []
    # #32：junction/软链一律不跟随——改写目标必须真实位于 md 根内
    for p in sorted(paths.walk_files(MD_ROOT, (".md",))):
        old_bytes = p.read_bytes()
        text = old_bytes.decode("utf-8", errors="replace")
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
            rel = p.relative_to(MD_ROOT).as_posix()
            bak = BACKUP / backup_name(rel, old_bytes)
            if not bak.exists():
                bak.write_bytes(old_bytes)
            # 备份必须逐字节等于将被替换的原文，否则不改写（可回滚是硬前提）
            if bak.read_bytes() != old_bytes:
                backup_failed.append(rel)
                continue
            atomic.atomic_write_text(p, new, encoding="utf-8")
    stats = {"dry_run": args.dry_run, "files_changed": files_changed,
             "refs_converted": refs_ok, "refs_skipped": refs_skip,
             "skip_samples": skip_samples,
             "backup_failed": backup_failed,
             "elapsed_sec": round(time.perf_counter() - started, 1),
             "backup": str(BACKUP) if not args.dry_run else None}
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 1 if backup_failed else 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
