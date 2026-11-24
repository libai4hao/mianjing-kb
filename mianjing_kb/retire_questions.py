# -*- coding: utf-8 -*-
"""retire_questions：按 doc_hash 退役整篇文档的题目。

背景（P0）：`build_db` 的发布池会按排除表挡掉退役源（`rejected`/`superseded`…）的产物，
而历史库里的题还在，`refresh` 拷向量时就会撞上「现行库 N 行与影子库不匹配」而 ABORT。
本命令把同一批 doc_hash 的题从 kb.sqlite 与线上检索库一起删干净，让两边题集重新一致，
refresh 从而可以走 copy-vec（不必 `--reembed`）。

删除范围：`questions`、`questions_fts`（有则删该行）、`vec_questions`（有则删该 rowid）、
`canonical_dependents`/`question_variants`（按 doc_hash），并把因此变空的 `canonical_questions` 清掉。
`label_facts` 按 q_norm_hash 记账、可能被别的文档共用，**不动**，只在输出里报告命中数。

用法：
    python -m mianjing_kb.retire_questions --db <kb.sqlite> --doc-hash <h1> --doc-hash <h2>          # 预览
    python -m mianjing_kb.retire_questions --db <kb.sqlite> --doc-hash <h1> --doc-hash <h2> --apply  # 执行

默认 dry-run；`--apply` 才写。写库走与 intake/extract 共用的写锁（kb.sqlite 用 `kb`，检索库用 `online`）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from mianjing_kb.kb import db as kbdb, wlock  # noqa: E402

_MAX_PRINT = 10
NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _ph(n: int) -> str:
    return ",".join("?" * n)


def _affected_canonical_ids(conn: sqlite3.Connection, tables: set[str],
                            doc_hashes: list[str], q_keys: list[str]) -> set[str]:
    affected: set[str] = set()
    placeholders = _ph(len(doc_hashes))
    for table in ("canonical_dependents", "question_variants"):
        if table in tables:
            affected.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT canonical_id FROM {table} WHERE doc_hash IN ({placeholders})",
                doc_hashes))
    if "merge_candidates" in tables and q_keys:
        affected.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT canonical_id FROM merge_candidates WHERE q_key IN ({_ph(len(q_keys))})",
            q_keys))
    return affected


def plan(conn: sqlite3.Connection, doc_hashes: list[str]) -> dict:
    """只读统计：返回各表将删除的行数与 qid 列表。"""
    tables = _tables(conn)
    hs = list(doc_hashes)
    qids = [r[0] for r in conn.execute(
        f"SELECT qid FROM questions WHERE doc_hash IN ({_ph(len(hs))})", hs)]
    q_keys = [r[0] for r in conn.execute(
        f"SELECT q_key FROM questions WHERE doc_hash IN ({_ph(len(hs))})", hs) if r[0]]
    out: dict = {"doc_hashes": hs, "qids": qids, "questions": len(qids), "tables": {}}
    if "questions_fts" in tables:
        out["tables"]["questions_fts"] = conn.execute(
            f"SELECT COUNT(*) FROM questions_fts WHERE rowid IN ({_ph(len(qids))})", qids).fetchone()[0] if qids else 0
    if "vec_questions" in tables:
        try:
            out["tables"]["vec_questions"] = conn.execute(
                f"SELECT COUNT(*) FROM vec_questions WHERE rowid IN ({_ph(len(qids))})", qids).fetchone()[0] if qids else 0
        except sqlite3.OperationalError:
            out["tables"]["vec_questions"] = None      # 未加载 sqlite-vec，按 rowid 删除仍会执行
    for t in ("canonical_dependents", "question_variants"):
        if t in tables:
            out["tables"][t] = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE doc_hash IN ({_ph(len(hs))})", hs).fetchone()[0]
    if "merge_candidates" in tables and q_keys:
        out["tables"]["merge_candidates"] = conn.execute(
            f"SELECT COUNT(*) FROM merge_candidates WHERE q_key IN ({_ph(len(q_keys))})", q_keys
        ).fetchone()[0]
    if "canonical_questions" in tables:
        out["canonical_questions_affected"] = len(
            _affected_canonical_ids(conn, tables, hs, q_keys))
    if "label_facts" in tables and qids:
        names = {r[1] for r in conn.execute("PRAGMA table_info(label_facts)")}
        if "q_norm_hash" in names:
            hashes = [r[0] for r in conn.execute(
                f"SELECT q_norm_hash FROM questions WHERE qid IN ({_ph(len(qids))})", qids) if r[0]]
            out["label_facts_touched"] = conn.execute(
                f"SELECT COUNT(*) FROM label_facts WHERE q_norm_hash IN ({_ph(len(hashes))})", hashes
            ).fetchone()[0] if hashes else 0
    return out


def retire(conn: sqlite3.Connection, doc_hashes: list[str], apply: bool = False) -> dict:
    """按 doc_hash 退役题目；apply=False 只统计。调用方负责事务与写锁。"""
    res = plan(conn, doc_hashes)
    res["applied"] = False
    if not apply or not res["questions"]:
        res["applied"] = bool(apply and not res["questions"])
        return res
    tables = _tables(conn)
    hs, qids = res["doc_hashes"], res["qids"]
    q_keys = [r[0] for r in conn.execute(
        f"SELECT q_key FROM questions WHERE doc_hash IN ({_ph(len(hs))})", hs) if r[0]]
    affected = _affected_canonical_ids(conn, tables, hs, q_keys)
    if "questions_fts" in tables:
        conn.execute(f"DELETE FROM questions_fts WHERE rowid IN ({_ph(len(qids))})", qids)
    if "vec_questions" in tables:
        try:
            conn.execute(f"DELETE FROM vec_questions WHERE rowid IN ({_ph(len(qids))})", qids)
        except sqlite3.OperationalError:
            pass                                    # 虚表模块没加载：refresh 会整表重建向量
    for t in ("canonical_dependents", "question_variants"):
        if t in tables:
            conn.execute(f"DELETE FROM {t} WHERE doc_hash IN ({_ph(len(hs))})", hs)
    res["merge_candidates_removed"] = 0
    if "merge_candidates" in tables and q_keys:
        cur = conn.execute(
            f"DELETE FROM merge_candidates WHERE q_key IN ({_ph(len(q_keys))})", q_keys)
        res["merge_candidates_removed"] = cur.rowcount
    conn.execute(f"DELETE FROM questions WHERE doc_hash IN ({_ph(len(hs))})", hs)
    if "canonical_questions" in tables and affected:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(canonical_questions)")}
        empty_ids = []
        for cid in sorted(affected):
            has_dependents = ("canonical_dependents" in tables and conn.execute(
                "SELECT 1 FROM canonical_dependents WHERE canonical_id=? LIMIT 1", (cid,)).fetchone())
            has_variants = ("question_variants" in tables and conn.execute(
                "SELECT 1 FROM question_variants WHERE canonical_id=? LIMIT 1", (cid,)).fetchone())
            has_candidates = ("merge_candidates" in tables and conn.execute(
                "SELECT 1 FROM merge_candidates WHERE canonical_id=? LIMIT 1", (cid,)).fetchone())
            if not has_dependents and not has_variants and not has_candidates:
                empty_ids.append(cid)
        removed = 0
        if empty_ids:
            removed = conn.execute(
                f"DELETE FROM canonical_questions WHERE canonical_id IN ({_ph(len(empty_ids))})",
                empty_ids).rowcount
        res["canonical_questions_removed"] = removed
        if "witness" in columns:
            for cid in sorted(affected - set(empty_ids)):
                witness = (conn.execute(
                    "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(collection),''),'doc:'||doc_hash)) "
                    "FROM canonical_dependents WHERE canonical_id=?", (cid,)).fetchone()[0]
                    if "canonical_dependents" in tables else 0)
                if "updated_at" in columns:
                    conn.execute("UPDATE canonical_questions SET witness=?, updated_at=? WHERE canonical_id=?",
                                 (witness, datetime.now().strftime(NOW_FMT), cid))
                else:
                    conn.execute("UPDATE canonical_questions SET witness=? WHERE canonical_id=?",
                                 (witness, cid))
    res["applied"] = True
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按 doc_hash 退役题目（questions/FTS/向量/canonical 依赖）")
    ap.add_argument("--db", required=True, help="目标库（kb.sqlite 或 面经库.db）")
    ap.add_argument("--doc-hash", action="append", required=True, help="要退役的 doc_hash，可给多次")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(json.dumps({"error": f"数据库不存在：{path}"}, ensure_ascii=False))
        return 2
    lock_name = wlock.lock_name_for_target(path)
    try:
        if args.apply:
            with wlock.write_lock(lock_name, tool="retire_questions", build_dir=path.parent,
                                  wait_sec=30.0, target=path):
                backup = kbdb.backup_db(path)
                conn = sqlite3.connect(str(path))
                conn.row_factory = sqlite3.Row
                try:
                    with conn:
                        res = retire(conn, args.doc_hash, apply=True)
                    res["backup"] = str(backup)
                finally:
                    conn.close()
        else:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                res = retire(conn, args.doc_hash, apply=False)
            finally:
                conn.close()
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False, indent=1))
        return 2
    if len(res.get("qids", [])) > _MAX_PRINT:
        res["qids"] = res["qids"][:_MAX_PRINT] + ["…"]
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
