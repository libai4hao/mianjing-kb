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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, labelspec, vocab  # noqa: E402


def clean(conn, apply: bool = False) -> dict:
    cap = labelspec.tag_limits()[0]
    stop = vocab.tag_stopwords()
    rows = conn.execute("SELECT qid, tags FROM labels_staging "
                        "WHERE COALESCE(tags,'') != ''").fetchall()
    changed = dropped_total = truncated_total = 0
    for r in rows:
        qid, raw = r["qid"], r["tags"]
        try:
            tags = json.loads(raw) or []
        except (TypeError, ValueError):
            continue
        kept = [t for t in (str(x).strip() for x in tags) if t and t not in stop]
        dropped = len(tags) - len(kept)
        cut = max(0, len(kept) - cap)
        kept = kept[:cap]
        if not dropped and not cut:
            continue
        changed += 1
        dropped_total += dropped
        truncated_total += cut
        if apply:
            conn.execute("UPDATE labels_staging SET tags=? WHERE qid=?",
                         (json.dumps(kept, ensure_ascii=False) if kept else None, qid))
    if apply:
        conn.commit()
    return {"rows": len(rows), "changed": changed, "dropped_dimension": dropped_total,
            "truncated": truncated_total, "cap": cap, "applied": apply}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清洗 staging 标签（去维度词 + 截断）")
    ap.add_argument("--apply", action="store_true", help="写回；默认只统计")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)
    conn = db.connect(Path(args.db), create=False)
    try:
        print(json.dumps(clean(conn, args.apply), ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
