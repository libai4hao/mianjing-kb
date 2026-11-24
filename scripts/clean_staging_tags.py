# -*- coding: utf-8 -*-
"""clean_staging_tags：对已打标但未 merge 的 staging 行做标签维度清洗。

背景（docs/50 §7）：726 题 5,403 次标签里 37.8% 是 topic/组名/qtype/position 的重复，
平均值 7.44 个/题（上限 8），2,187 个不同标签里 69.7% 只出现一次。改 prompt 只影响
之后的行，已跑出来的行用这个脚本统一收拾：

  1. 剔除维度词（`kb.vocab.tag_stopwords()`：topic + 一级组名 + qtype + position）；
  2. 按 `meta/label_schema.yaml` 的 tags.max_items 截断（现为 4）；
  3. 全空的行写 NULL（merge 的 COALESCE 不覆盖旧值）。

用法：
    python scripts/clean_staging_tags.py            # 只统计，不改
    python scripts/clean_staging_tags.py --apply     # 写回 staging
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, labelspec, vocab, wlock  # noqa: E402


def clean(conn, apply: bool = False) -> dict:
    cap = labelspec.tag_limits()[0]
    stop = vocab.tag_stopwords()
    rows = conn.execute("SELECT qid, tags FROM labels_staging "
                        "WHERE COALESCE(tags,'') != ''").fetchall()
    changed = dropped_total = truncated_total = syn_total = dedup_total = 0
    for r in rows:
        qid, raw = r["qid"], r["tags"]
        try:
            tags = json.loads(raw) or []
        except (TypeError, ValueError):
            continue
        # 复用产品口径：同义归一 / 去重 / 维度词（含大小写变体）/ 空值，避免脚本与线上两套逻辑
        kept, syn, dropped = labelspec.normalize_tags(
            tags, max_items=len(tags) + 1, stopwords=stop)
        cut = max(0, len(kept) - cap)
        kept = kept[:cap]
        dedup = max(0, len(tags) - dropped - len(kept) - cut)
        if not (dropped or cut or syn or dedup):
            continue
        changed += 1
        dropped_total += dropped
        truncated_total += cut
        syn_total += syn
        dedup_total += dedup
        if apply:
            conn.execute("UPDATE labels_staging SET tags=? WHERE qid=?",
                         (json.dumps(kept, ensure_ascii=False) if kept else None, qid))
    return {"rows": len(rows), "changed": changed, "dropped_dimension": dropped_total,
            "truncated": truncated_total, "synonym_hits": syn_total,
            "deduped": dedup_total, "cap": cap, "applied": apply}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清洗 staging 标签（去维度词 + 截断）")
    ap.add_argument("--apply", action="store_true", help="写回；默认只统计")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)
    path = Path(args.db)
    lock = nullcontext()
    if args.apply:
        lock = wlock.write_lock(wlock.lock_name_for_target(path), tool="clean_staging_tags",
                                build_dir=path.parent, wait_sec=30.0, target=path)
    try:
        with lock:
            conn = db.connect(path, create=False, readonly=not args.apply)
            try:
                if args.apply:
                    backup = db.backup_db(path)
                    with conn:
                        out = clean(conn, True)
                    out["backup"] = str(backup)
                else:
                    out = clean(conn, False)
                print(json.dumps(out, ensure_ascii=False, indent=1))
            finally:
                conn.close()
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False, indent=1))
        return 2
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
