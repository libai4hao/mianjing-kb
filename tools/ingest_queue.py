# -*- coding: utf-8 -*-
"""queue：把"确认可入库"的文档导成清单表（CSV / Markdown），并给出分布。

清单字段：文件名、父目录名、hash、相对路径、扩展名、大小、类别、主题、来源集、可信度。
用途有两个：
  1. 人眼核对——这一批到底要入哪些；
  2. 快速去重——新文件进 `31-收件箱` 前，可以按 hash 直接比对这张表。

数据源是视图 `ingest_queue`（status='classified'，已剔除 rejected / quarantined / duplicate）。

用法：
    python tools/ingest_queue.py --export build\\ingest_list.csv
    python tools/ingest_queue.py --stats
    python tools/ingest_queue.py --lookup "某个hash或文件名"
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db  # noqa: E402

COLUMNS = ["doc_hash", "file_name", "parent_dir", "dir_path", "ext", "size_bytes",
           "category", "topic", "collection", "source_trust"]


def fetch(conn, limit: int = 0) -> list[dict]:
    sql = f"SELECT {','.join(COLUMNS)} FROM ingest_queue ORDER BY dir_path, file_name"
    if limit:
        sql += f" LIMIT {limit}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可入库清单：导出 / 统计 / 查重")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--export", default=None, help="导出 CSV 路径")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--lookup", default=None, help="按 hash 或文件名片段查询")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=False)
    rows = fetch(conn, args.limit)

    if args.lookup:
        key = args.lookup.lower()
        hit = [r for r in rows if key in (r["doc_hash"] or "").lower() or key in (r["file_name"] or "").lower()]
        print(f"[lookup] 命中 {len(hit)} 条")
        for r in hit[:20]:
            print(f"  {r['doc_hash']}  {r['parent_dir']:<24} {r['file_name']}")
        if not hit:
            q = conn.execute("SELECT doc_hash, file_name, status FROM source_documents "
                             "WHERE doc_hash LIKE ? OR file_name LIKE ? LIMIT 20",
                             (f"%{key}%", f"%{key}%")).fetchall()
            for r in q:
                print(f"  (库内非可入库) {r['doc_hash']} {r['file_name']} status={r['status']}")

    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for r in rows:
                r = dict(r)
                r["size_bytes"] = r["size_bytes"]
                w.writerow(r)
        print(f"[export] {len(rows)} 行 → {out}")

    if args.stats or not (args.export or args.lookup):
        by_cat = {}
        by_parent = {}
        by_topic = {}
        for r in rows:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
            by_parent[r["parent_dir"]] = by_parent.get(r["parent_dir"], 0) + 1
            by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
        summary = {
            "db": args.db,
            "queue_rows": len(rows),
            "hash_unique": len({r["doc_hash"] for r in rows}),
            "category": by_cat,
            "parent_dir_top15": dict(sorted(by_parent.items(), key=lambda x: -x[1])[:15]),
            "topic_top10": dict(sorted(by_topic.items(), key=lambda x: -x[1])[:10]),
            "total_mb": round(sum(r["size_bytes"] for r in rows) / 1048576, 1),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
