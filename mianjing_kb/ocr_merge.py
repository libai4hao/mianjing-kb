# -*- coding: utf-8 -*-
"""OCR 定稿合并：把 build/ocr_text/<rel>.txt 回填为该源在 20-题库MD 的正式 md。

- 合并集 = gate_report fail/review 行 ∩ ocr_text 已有全文（dispatch 的 ocr_done + 新跑完的）
- 旧 md 先备份到 build/ocr_md_backup/（posix rel 落盘），可整目录回滚
- 新 md = OCR 全文（带 ===== page n ===== 页标记，保留回溯能力）+ 原 md 的「## 配图」段（图片引用不丢）
- 不动 gate/extract——合并后由调用方重跑

用法：
    python -m mianjing_kb.ocr_merge --dry-run     # 只列合并集与配图段检测结果
    python -m mianjing_kb.ocr_merge               # 实际合并（幂等：重复跑=同内容重写）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import atomic, config, paths  # noqa: E402

BUILD = Path(config.BUILD_DIR)   # P1-3 修复（2026-11-17 审核）：路径统一走 config，KB_* 环境变量可覆盖
OCR_TEXT = BUILD / "ocr_text"
BACKUP = BUILD / "ocr_md_backup"
IMG_MARK = "## 配图"


def _trusted_md_path(raw: str | Path) -> Path:
    """把报告中的 md 指向 QUESTION_MD_DIR 内部；报告字段本身不作为信任依据。"""
    root = Path(config.QUESTION_MD_DIR).resolve()
    value = str(raw or "")
    if not value or "\x00" in value:
        raise paths.UnsafePath("md 目标为空或含 NUL")
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as ex:
            raise paths.UnsafePath(f"报告 md 越出题库根目录: {value}") from ex
        return resolved
    return paths.safe_join(root, value.replace("\\", "/"))


def merge_set(report_path: Path) -> list[dict]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    out = []
    rows = report.get("rows") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise ValueError("gate report 缺少 rows 列表")
    for r in rows:
        if not isinstance(r, dict) or r.get("state") not in ("fail", "review"):
            continue
        raw_rel = r.get("rel")
        if not isinstance(raw_rel, str) or not raw_rel:
            continue
        rel = raw_rel.replace("\\", "/")
        try:
            tp = paths.safe_join(OCR_TEXT, rel + ".txt", must_exist=True)
            md = _trusted_md_path(r.get("md"))
            pdf = paths.safe_join(config.CORPUS_ROOT, rel, must_exist=True)
        except paths.UnsafePath:
            continue
        # 只合并具有完整页清单、源 PDF 指纹和输出 hash 的 OCR 产物。
        from mianjing_kb.ocr_pages import is_complete_text
        if tp.stat().st_size > 0 and is_complete_text(rel, pdf, OCR_TEXT) and md.is_file():
            out.append({"rel": rel, "md": str(md), "ocr_txt": str(tp),
                        "reason": r.get("reason", "")})
    return out


def backup_name(rel: str, old_bytes: bytes) -> str:
    """rel + 原文内容双哈希，避免 a/b_c.md 与 a_b/c.md 扁平化后撞名。"""
    return (hashlib.sha256(rel.replace("\\", "/").encode("utf-8")).hexdigest()[:16] + "-"
            + hashlib.sha256(old_bytes).hexdigest()[:16] + ".md")


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
            old_bytes = old_md.read_bytes()
            bak_name = backup_name(j["rel"], old_bytes)
            bak = BACKUP / bak_name
            if not bak.exists():
                shutil.copy2(old_md, bak)
            if hashlib.sha256(bak.read_bytes()).hexdigest() != \
                    hashlib.sha256(old_bytes).hexdigest():
                raise RuntimeError(f"OCR 备份内容校验失败: {bak}")
            atomic.atomic_write_text(old_md, new_md, encoding="utf-8")
        results.append(rec)
    elapsed = time.perf_counter() - started
    ok_n = sum(1 for r in results if r.get("ok"))
    print(json.dumps({"jobs": len(jobs), "merged": ok_n if not args.dry_run else 0,
                      "dry_run": args.dry_run, "elapsed_sec": round(elapsed, 1),
                      "results": results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
