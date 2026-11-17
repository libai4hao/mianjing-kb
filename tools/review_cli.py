# -*- coding: utf-8 -*-
"""P2-③ review_cli：灰区件人工复核入口。

灰区桶：
  review    dispatch 判 review 的件（需人工裁定）
  suggest   dispatch 判 supersede_suggest 的件（建议退役，待确认）
  quarantined 隔离区件（source_documents.status='quarantined'）
  lowconf   conf='low' 的低信度题（只读展示，不改）

用法：
    python tools/review_cli.py                       # 全灰区汇总
    python tools/review_cli.py --list suggest        # 看某桶明细
    python tools/review_cli.py --resolve <rel或file_name> --status superseded --note "退役：V1.1 已替换"
        # 只允许 classified/superseded/rejected 三态；note 自动带时间戳留痕
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config, db  # noqa: E402

RESOLVABLE = {"classified", "superseded", "rejected"}


def _plan_rows() -> list[dict]:
    plan = Path(config.BUILD_DIR) / "dispatch_plan.csv"
    if not plan.exists():
        return []
    return list(csv.DictReader(plan.open(encoding="utf-8-sig")))


def gray(conn) -> dict:
    plan = _plan_rows()
    buckets = {
        "review": [r for r in plan if r["route"] == "review"],
        "suggest": [r for r in plan if r["route"] == "supersede_suggest"],
        "quarantined": [dict(r) for r in conn.execute(
            "SELECT doc_hash, file_name, dir_path, note FROM source_documents "
            "WHERE status='quarantined'").fetchall()],
    }
    lowconf = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE conf='low'").fetchone()[0]
    return {k: {"count": len(v), "rows": v} for k, v in buckets.items()} | {
        "lowconf": {"count": lowconf, "rows": [
            dict(r) for r in conn.execute(
                "SELECT qid, doc_hash, q_text, qtype FROM questions WHERE conf='low' LIMIT 10").fetchall()]}}


def resolve(conn, key: str, status: str, note: str) -> dict:
    if status not in RESOLVABLE:
        return {"ok": False, "err": f"status 只允许 {sorted(RESOLVABLE)}"}
    rows = conn.execute(
        "SELECT doc_hash, file_name, status FROM source_documents "
        "WHERE src_path LIKE ? OR file_name LIKE ?", (f"%{key}%", f"%{key}%")).fetchall()
    if not rows:
        return {"ok": False, "err": f"未找到匹配件: {key}"}
    if len(rows) > 1:
        return {"ok": False, "err": f"匹配到 {len(rows)} 件，请用更精确关键字",
                "candidates": [r["file_name"] for r in rows]}
    row = rows[0]
    stamp = datetime.now().strftime("%Y-%m-%d")
    new_note = f"[review_cli {stamp}] {status}: {note}".strip()
    conn.execute("UPDATE source_documents SET status=?, note=? WHERE doc_hash=?",
                 (status, new_note, row["doc_hash"]))
    conn.commit()
    return {"ok": True, "file_name": row["file_name"],
            "old_status": row["status"], "new_status": status, "note": new_note}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="灰区复核")
    ap.add_argument("--list", choices=["review", "suggest", "quarantined", "lowconf"])
    ap.add_argument("--resolve", default=None, help="rel 尾段或 file_name 关键字")
    ap.add_argument("--status", default=None, choices=sorted(RESOLVABLE))
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    conn = db.connect(config.DB_PATH, create=False)
    if args.resolve:
        if not args.status:
            print(json.dumps({"ok": False, "err": "--resolve 需配合 --status"}, ensure_ascii=False))
            return 2
        out = resolve(conn, args.resolve, args.status, args.note)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["ok"] else 2
    g = gray(conn)
    conn.close()
    if args.list:
        print(json.dumps({args.list: g[args.list]}, ensure_ascii=False, indent=1))
    else:
        print(json.dumps({k: v["count"] for k, v in g.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
