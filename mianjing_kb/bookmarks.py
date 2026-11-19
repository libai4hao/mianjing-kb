# -*- coding: utf-8 -*-
"""扫描 PDF 书签，把"合并版 → 源文件名 → 页区间"落库。

R2 §5.2 / ADR D-16：合并轮把 110 个源并入 23 份合并版，md 侧已无源边界
（0 个一级标题、源名无痕迹），唯一载体是 PDF TOC 的 L1 书签。
**删除 99-重复文件待删 之前必须先跑本脚本**，否则见证数不可恢复。

用法：
    python -m mianjing_kb.bookmarks                 # 只补未扫描的 classified PDF
    python -m mianjing_kb.bookmarks --force         # 全部重扫（书签变了就重跑）
    python -m mianjing_kb.bookmarks --only-merged   # 只处理 source_count>1 的
    python -m mianjing_kb.bookmarks --dry-run       # 只统计不写库
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf  # noqa: E402

from mianjing_kb.kb import config, db, status, wlock  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"

# 源边界的判据：L1 标题必须形如文件名（合并工具写入的就是源文件名）。
# 普通 PDF 的 L1 是章节标题（"第 1 章 Java 概述"），不能当源——干跑实测过：
# 若把"L1 条数"一律当源数，291 份 PDF 会算出 119 份"合并版 / 3,338 个源"。
SRC_TITLE_RE = re.compile(r"\.(pdf|docx|doc|md|pptx|txt)$", re.I)


def spans_from_toc(toc: list, page_count: int, file_name: str) -> tuple[int, list[dict]]:
    """把 TOC 的 L1 条目转成 [{name, page_start, page_end}]（页号 1-based，闭区间）。

    只认"标题形如文件名"的 L1 条目作为源边界；没有这类条目就按单源处理并留痕。
    """
    l1 = [(t[1], t[2]) for t in toc if t[0] == 1 and t[2] and SRC_TITLE_RE.search((t[1] or "").strip())]
    if not l1:
        # 没有源级书签：按单源处理并留痕，绝不把它当成"1 个源"却不说明来源
        return 1, [{"name": file_name, "page_start": 1, "page_end": page_count, "from": "fallback_no_source_toc"}]
    out = []
    for i, (name, start) in enumerate(l1):
        end = (l1[i + 1][1] - 1) if i + 1 < len(l1) else page_count
        if end < start:
            end = start
        out.append({"name": name, "page_start": start, "page_end": end})
    return len(l1), out


def _run(args) -> int:
    conn = db.connect(args.db, create=not args.dry_run, readonly=args.dry_run)
    sql = (
        "SELECT doc_hash, file_name, src_path, dir_path, source_count "
        f"FROM source_documents WHERE ext = '.pdf' AND status = '{status.CLASSIFIED}' "
        f"AND dir_path NOT LIKE '{config.QUARANTINE_PREFIX}%'"
    )
    rows = [dict(r) for r in conn.execute(sql)]

    scanned = merged = sources_total = no_toc = missing = skipped = 0
    started = time.perf_counter()
    merged_rows = []

    for r in rows:
        p = Path(r["src_path"] or "")
        if not p.exists():
            missing += 1
            continue
        if args.only_merged and (r["source_count"] or 0) <= 1:
            skipped += 1
            continue
        if r["source_count"] is not None and not args.force and not args.only_merged:
            skipped += 1
            continue
        try:
            with pymupdf.open(p) as doc:
                page_count = doc.page_count
                toc = doc.get_toc()
        except Exception as exc:  # 损坏 PDF 不能阻塞整批
            print(f"[warn] 打不开: {r['file_name']} ({exc})", file=sys.stderr)
            missing += 1
            continue

        count, spans = spans_from_toc(toc, page_count, r["file_name"])
        scanned += 1
        sources_total += count
        if count > 1:
            merged += 1
            merged_rows.append((r["file_name"], page_count, count))
        if count == 1 and "from" in spans[0]:
            no_toc += 1
        if not args.dry_run:
            conn.execute(
                "UPDATE source_documents SET source_count=?, sources_json=?, last_seen_at=? WHERE doc_hash=?",
                (count, json.dumps(spans, ensure_ascii=False), datetime.now().strftime(NOW_FMT), r["doc_hash"]),
            )

    if not args.dry_run:
        conn.commit()

    for name, pages, count in sorted(merged_rows, key=lambda x: -x[2]):
        print(f"  {name[:34]:34s} {pages:>5}p  源 {count:>2}")

    out = {
        "ok": True,
        "dry_run": args.dry_run,
        "pdf_rows": len(rows),
        "scanned": scanned,
        "merged_docs": merged,
        "sources_in_merged": sum(c for _, _, c in merged_rows),
        "single_source": scanned - merged,
        "fallback_no_toc": no_toc,
        "skipped_already_done": skipped,
        "missing_or_broken": missing,
        "elapsed_s": round(time.perf_counter() - started, 2),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="扫描 PDF 书签并落 source_count/sources_json")
    ap.add_argument("--db", default=str(config.DB_PATH), help="数据库路径")
    ap.add_argument("--force", action="store_true", help="已扫过的也重扫")
    ap.add_argument("--only-merged", action="store_true", help="只处理 source_count>1 的")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args(argv)
    try:
        if not args.dry_run:
            with wlock.write_lock("kb", tool="bookmarks", build_dir=Path(args.db).parent,
                                  target=args.db):
                return _run(args)
        return _run(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False, indent=1),
              file=sys.stderr)
        return 2




if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
