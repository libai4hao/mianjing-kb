# -*- coding: utf-8 -*-
"""OCR 定稿合并：把 build/ocr_text/<rel>.txt 回填为该源在 20-题库MD 的正式 md。

- 合并集 = gate_report fail/review 行 ∩ ocr_text 已有全文（dispatch 的 ocr_done + 新跑完的）
- 旧 md 先备份到 build/ocr_md_backup/（posix rel 落盘），可整目录回滚
- 新 md = OCR 全文（带 ===== page n ===== 页标记，保留回溯能力）+ 原 md 的「## 配图」段（图片引用不丢）
- 不动 gate/extract——合并后由调用方重跑

用法：
    python tools/ocr_merge.py --dry-run     # 只列合并集与配图段检测结果
    python tools/ocr_merge.py               # 实际合并（幂等：重复跑=同内容重写）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config  # noqa: E402

BUILD = Path(r"<CORPUS_ROOT>\21-面经数据库\build")
OCR_TEXT = BUILD / "ocr_text"
BACKUP = BUILD / "ocr_md_backup"
IMG_MARK = "## 配图"


def merge_set(report_path: Path) -> list[dict]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    out = []
    for r in report["rows"]:
        if r["state"] not in ("fail", "review"):
            continue
        rel = r["rel"].replace("\\", "/")
        tp = OCR_TEXT / (rel + ".txt")
        if tp.exists() and tp.stat().st_size > 0 and r.get("md"):
            out.append({"rel": rel, "md": r["md"], "ocr_txt": str(tp),
                        "reason": r.get("reason", "")})
    return out


def split_images(old_md: str) -> tuple[str, str]:
    """拆出「## 配图」段（含其后的全部内容），返回 (正文, 配图段)。"""
    idx = old_md.find(IMG_MARK)
    if idx < 0:
        return old_md, ""
    # 配图段前面通常有 "---" 分隔线，一并保留
    start = old_md.rfind("---", 0, idx)
    start = start if start >= 0 else idx
    return old_md[:start].rstrip() + "\n", old_md[start:].strip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OCR 文本定稿合并回 md")
    ap.add_argument("--report", default=str(BUILD / "gate_report.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    jobs = merge_set(Path(args.report))
    started = time.perf_counter()
    results = []
    for j in jobs:
        old_md = Path(j["md"])
        tp = Path(j["ocr_txt"])
        if not old_md.exists():
            results.append({"rel": j["rel"], "ok": False, "err": "md 不存在"})
            continue
        ocr_text = tp.read_text(encoding="utf-8", errors="replace").strip()
        body, img_sec = split_images(old_md.read_text(encoding="utf-8", errors="replace"))
        new_md = ocr_text + ("\n\n" + img_sec if img_sec else "") + "\n"
        rec = {"rel": j["rel"], "ok": True, "old_chars": len(body),
               "new_chars": len(new_md), "imgs_kept": bool(img_sec)}
        if not args.dry_run:
            BACKUP.mkdir(parents=True, exist_ok=True)
            bak = BACKUP / (j["rel"].replace("/", "_"))
            if not bak.exists():
                shutil.copy2(old_md, bak)
            old_md.write_text(new_md, encoding="utf-8")
        results.append(rec)
    elapsed = time.perf_counter() - started
    ok_n = sum(1 for r in results if r.get("ok"))
    print(json.dumps({"jobs": len(jobs), "merged": ok_n if not args.dry_run else 0,
                      "dry_run": args.dry_run, "elapsed_sec": round(elapsed, 1),
                      "results": results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
