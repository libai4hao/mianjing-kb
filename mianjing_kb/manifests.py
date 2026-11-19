# -*- coding: utf-8 -*-
"""T29：批次 manifest 与按批回滚。

manifest 是"这批增量到底改了什么"的账本：
  - created_canonicals：本批新建的 canonical_id
  - attached：吸附到既有 canonical 的 q_key
  - candidates：0.65–0.7 待人工合并的候选
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


def _write(data: dict, build_dir: Path | None = None) -> Path:
    p = _path(data["batch_id"], build_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def create(batch_id: str, payload: dict, build_dir: Path | None = None) -> dict:
    """兼容旧调用：直接写 committed。新批次请用 prepare() + mark_committed()。"""
    d = manifest_dir(build_dir)
    d.mkdir(parents=True, exist_ok=True)
    data = {"batch_id": batch_id, "tool": payload.get("tool", "canonical"),
            "status": "committed", "created_at": datetime.now().strftime(NOW_FMT),
            **payload}
    _write(data, build_dir)
    return data


def prepare(batch_id: str, payload: dict, build_dir: Path | None = None) -> dict:
    """写 prepared 清单：SQLite 尚未提交前绝不能标 committed。"""
    d = manifest_dir(build_dir)
    d.mkdir(parents=True, exist_ok=True)
    data = {"batch_id": batch_id, "tool": payload.get("tool", "canonical"),
            "status": "prepared", "created_at": datetime.now().strftime(NOW_FMT),
            "prepared_at": datetime.now().strftime(NOW_FMT), **payload}
    _write(data, build_dir)
    return data


def mark_committed(batch_id: str, build_dir: Path | None = None) -> dict | None:
    """SQLite commit 成功后调用；已 rolled_back 的批次不得被重新标 committed。"""
    data = load(batch_id, build_dir)
    if not data:
        return None
    if data.get("status") == "rolled_back":
        return data
    if data.get("status") == "committed":
        return data
    data["status"] = "committed"
    data["committed_at"] = datetime.now().strftime(NOW_FMT)
    _write(data, build_dir)
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


def _candidate_row(conn, qkey: str):
    row = conn.execute(
        "SELECT q_key, canonical_id, similarity, status, created_at, updated_at "
        "FROM merge_candidates WHERE q_key=?", (qkey,)).fetchone()
    if not row:
        return None
    return {"q_key": row[0], "canonical_id": row[1], "similarity": float(row[2]),
            "status": row[3], "created_at": row[4], "updated_at": row[5]}


def _rollback_candidates(conn, data: dict) -> tuple[int, int, list[dict]]:
    """只回滚仍属于本批次原始状态的行；人工改过（approved/rejected/merged）一律跳过。

    返回 (删除数, 恢复数, 跳过清单)。兼容旧 manifest：没有 candidates_prior 时不猜，
    宁可跳过也不把后来的人工状态覆盖掉。
    """
    prior_map = data.get("candidates_prior")
    if not isinstance(prior_map, dict):
        return 0, 0, [{"q_key": c.get("q_key"), "why": "旧 manifest 缺少 candidates_prior，拒绝猜测"}
                      for c in (data.get("candidates") or [])]
    removed = restored = 0
    skipped: list[dict] = []
    now = datetime.now().strftime(NOW_FMT)
    for c in data.get("candidates") or []:
        qkey = c.get("q_key")
        if not qkey or qkey not in prior_map:
            skipped.append({"q_key": qkey, "why": "manifest 缺少该候选的前态"})
            continue
        cur = _candidate_row(conn, qkey)
        if cur is None:
            continue
        same = (cur["canonical_id"] == c.get("canonical_id")
                and abs(cur["similarity"] - float(c.get("similarity", -1))) < 1e-9)
        if cur["status"] != "pending" or not same:
            skipped.append({"q_key": qkey, "why": "候选已被后续处理，保持现状",
                            "status": cur["status"]})
            continue
        prior = prior_map.get(qkey)
        if prior is None:
            conn.execute("DELETE FROM merge_candidates WHERE q_key=?", (qkey,))
            removed += 1
        else:
            conn.execute(
                "UPDATE merge_candidates SET canonical_id=?, similarity=?, status=?, "
                "created_at=?, updated_at=? WHERE q_key=?",
                (prior.get("canonical_id"), prior.get("similarity"), prior.get("status", "pending"),
                 prior.get("created_at"), prior.get("updated_at"), qkey))
            restored += 1
    # 兼容旧逻辑：added_variants 对应的候选若仍 pending 且没有前态记录，至少标 rolled_back
    qkeys = [x for x in (data.get("added_variants") or []) if x]
    if qkeys:
        conn.execute("UPDATE merge_candidates SET status='rolled_back', updated_at=? "
                     "WHERE q_key IN (SELECT value FROM json_each(?)) AND status='pending'",
                     (now, json.dumps(qkeys, ensure_ascii=False)))
    return removed, restored, skipped



def reconcile(conn, build_dir: Path | None = None, limit: int = 50) -> list[dict]:
    """对账未完成批次：prepared 是崩溃窗口，按库里实际状态判定 applied/partial/empty。

    不自动改 manifest、不自动删数据；先给出判断，人工再决定 mark_committed 或 rollback。
    """
    d = manifest_dir(build_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        data = load(p.stem, build_dir) or {}
        if data.get("status") != "prepared":
            continue
        variants = [q for q in (data.get("added_variants") or []) if q]
        present_variants = 0
        for qkey in variants:
            if conn.execute("SELECT 1 FROM question_variants WHERE q_key=? LIMIT 1",
                             (qkey,)).fetchone():
                present_variants += 1
        cands = data.get("candidates") or []
        present_cands = 0
        for c in cands:
            cur = _candidate_row(conn, c.get("q_key"))
            if cur and cur["canonical_id"] == c.get("canonical_id"):
                present_cands += 1
        if not variants and not cands:
            verdict = "empty"
        elif present_variants == len(variants) and present_cands == len(cands):
            verdict = "applied"
        elif present_variants == 0 and present_cands == 0:
            verdict = "not_applied"
        else:
            verdict = "partial"
        out.append({"batch_id": p.stem, "prepared_at": data.get("prepared_at"),
                    "variants": [present_variants, len(variants)],
                    "candidates": [present_cands, len(cands)], "verdict": verdict})
    return out


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
    removed_candidates, restored_candidates, skipped_candidates = _rollback_candidates(conn, data)
    conn.commit()
    data["status"] = "rolled_back"
    data["rolled_back_at"] = datetime.now().strftime(NOW_FMT)
    data["rollback_applied"] = True
    data["removed_variants"] = n_var
    data["removed_candidates"] = removed_candidates
    data["restored_candidates"] = restored_candidates
    data["skipped_candidates"] = skipped_candidates
    create(batch_id, data, build_dir)
    return {"ok": True, "batch_id": batch_id, "status": "rolled_back",
            "removed_variants": n_var, "touched_canonicals": len(touched),
            "removed_candidates": removed_candidates,
            "restored_candidates": restored_candidates,
            "skipped_candidates": skipped_candidates}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T29 增量批次 manifest：列表 / 回滚")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--reconcile", action="store_true", help="对账 prepared 未完成批次")
    ap.add_argument("--commit", metavar="BATCH_ID", help="确认已提交的 prepared 批次")
    ap.add_argument("--rollback", metavar="BATCH_ID")
    ap.add_argument("--apply", action="store_true", help="真正删本批 variant/dependent")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)
    if args.list:
        print(json.dumps({"manifests": list_manifests()}, ensure_ascii=False, indent=1))
        return 0
    if args.reconcile:
        conn = db.connect(args.db, create=False)
        try:
            out = reconcile(conn)
        finally:
            conn.close()
        print(json.dumps({"unfinished": out}, ensure_ascii=False, indent=1))
        return 0
    if args.commit:
        out = mark_committed(args.commit)
        print(json.dumps(out or {"ok": False, "why": "manifest 不存在"},
                         ensure_ascii=False, indent=1))
        return 0 if out else 2
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
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
