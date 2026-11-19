# -*- coding: utf-8 -*-
"""T29：批次 manifest 与按批回滚。

manifest 是"这批增量到底改了什么"的账本：
  - created_canonicals：本批新建的 canonical_id
  - attached：吸附到既有 canonical 的 q_key
  - candidates：0.85–0.92 待人工合并的候选
  - added_variants：本批新增的 variant q_key

回滚先标 `rollback_requested`（不动库）；带 `--apply` 才删本批 variant/dependent，
并只重算受影响 canonical 的 witness，不删任何源文件。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, wlock  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def manifest_dir(build_dir: Path | None = None) -> Path:
    return Path(build_dir or config.BUILD_DIR) / "manifests"


def _path(batch_id: str, build_dir: Path | None = None) -> Path:
    if not batch_id or "/" in batch_id or "\\" in batch_id or batch_id.startswith("."):
        raise ValueError(f"非法 batch_id: {batch_id!r}")
    return manifest_dir(build_dir) / f"{batch_id}.json"


def create(batch_id: str, payload: dict, build_dir: Path | None = None) -> dict:
    d = manifest_dir(build_dir)
    d.mkdir(parents=True, exist_ok=True)
    data = {"batch_id": batch_id, "tool": payload.get("tool", "canonical"),
            "status": "committed", "created_at": datetime.now().strftime(NOW_FMT),
            **payload}
    p = _path(batch_id, build_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return data


def load(batch_id: str, build_dir: Path | None = None) -> dict | None:
    try:
        return json.loads(_path(batch_id, build_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_manifests(build_dir: Path | None = None, limit: int = 20) -> list[dict]:
    d = manifest_dir(build_dir)
    if not d.exists():
        return []
    items = []
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        data = load(p.stem, build_dir) or {}
        items.append({"batch_id": p.stem, "status": data.get("status"),
                      "created_at": data.get("created_at"),
                      "created_canonicals": len(data.get("created_canonicals") or []),
                      "attached": len(data.get("attached") or []),
                      "candidates": len(data.get("candidates") or [])})
    return items


def _recompute_witness(conn, canonical_id: str) -> int:
    n = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(collection),''), 'doc:'||doc_hash)) "
        "FROM canonical_dependents WHERE canonical_id=?", (canonical_id,)).fetchone()[0]
    conn.execute("UPDATE canonical_questions SET witness=?, updated_at=? WHERE canonical_id=?",
                 (n, datetime.now().strftime(NOW_FMT), canonical_id))
    return int(n)


def rollback(conn, batch_id: str, apply: bool = False,
             build_dir: Path | None = None) -> dict:
    data = load(batch_id, build_dir)
    if not data:
        return {"ok": False, "why": "manifest 不存在", "batch_id": batch_id}
    if data.get("status") == "rolled_back":
        return {"ok": True, "batch_id": batch_id, "status": "already_rolled_back"}
    if not apply:
        data["status"] = "rollback_requested"
        data["requested_at"] = datetime.now().strftime(NOW_FMT)
        create(batch_id, data, build_dir)
        return {"ok": True, "batch_id": batch_id, "status": "rollback_requested",
                "note": "未动库；加 --apply 才执行回滚"}
    qkeys = [x for x in (data.get("added_variants") or []) if x]
    touched = set(data.get("touched_canonicals") or [])
    created = set(data.get("created_canonicals") or [])
    n_var = 0
    for qkey in qkeys:
        cur = conn.execute("DELETE FROM question_variants WHERE q_key=?", (qkey,))
        n_var += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.execute("DELETE FROM canonical_dependents WHERE q_key=?", (qkey,))
    for cid in created:
        left = conn.execute("SELECT COUNT(*) FROM question_variants WHERE canonical_id=?",
                            (cid,)).fetchone()[0]
        if left == 0:
            conn.execute("DELETE FROM canonical_questions WHERE canonical_id=?", (cid,))
        else:
            touched.add(cid)
    for cid in touched:
        if conn.execute("SELECT COUNT(*) FROM canonical_questions WHERE canonical_id=?",
                        (cid,)).fetchone()[0]:
            _recompute_witness(conn, cid)
    conn.execute("UPDATE merge_candidates SET status='rolled_back', updated_at=? "
                 "WHERE q_key IN (SELECT value FROM json_each(?))",
                 (datetime.now().strftime(NOW_FMT), json.dumps(qkeys, ensure_ascii=False)))
    conn.commit()
    data["status"] = "rolled_back"
    data["rolled_back_at"] = datetime.now().strftime(NOW_FMT)
    data["rollback_applied"] = True
    data["removed_variants"] = n_var
    create(batch_id, data, build_dir)
    return {"ok": True, "batch_id": batch_id, "status": "rolled_back",
            "removed_variants": n_var, "touched_canonicals": len(touched)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T29 增量批次 manifest：列表 / 回滚")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rollback", metavar="BATCH_ID")
    ap.add_argument("--apply", action="store_true", help="真正删本批 variant/dependent")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)
    if args.list:
        print(json.dumps({"manifests": list_manifests()}, ensure_ascii=False, indent=1))
        return 0
    if not args.rollback:
        ap.print_help()
        return 0
    db_path = Path(args.db)
    if not args.apply:
        out = rollback(db.connect(db_path, create=False), args.rollback, apply=False)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    try:
        with wlock.write_lock("kb", tool="manifest-rollback", build_dir=db_path.parent,
                              target=db_path):
            conn = db.connect(db_path, create=False)
            out = rollback(conn, args.rollback, apply=True)
            conn.close()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("ok") else 2
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False, indent=1))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
