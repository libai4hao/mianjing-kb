"""归一器升级时的 q_norm_hash 事务迁移（v1 → v2）。

用途：
- 先 dry-run 生成 old_hash → new_hash 快照，核对受影响题数和标签数；
- `--apply` 在同一事务里更新 questions.q_norm/q_norm_hash/q_key/simhash，以及
  label_facts.q_norm_hash/q_norm_sample/normalizer_version；
- question_variants / canonical 不在这里改：迁移完成后必须重建 canonical，否则旧
  variant 的 q_key 会悬空。

设计约束：
- 新口径可能让同一文档的多条题落到同一个 q_norm_hash；按 extract 同款“答案最长”
  保留一条，其余删除，并在报告里列出。
- label_facts 生产库当前为 0；若未来有标签，多个 old_hash 合并到同一 new_hash 时
  按 merged_at DESC, rowid DESC 保留一条，避免撞唯一索引。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from mianjing_kb.kb import config, db, wlock
from mianjing_kb.question_freq import normalize, simhash

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def _hash(norm: str) -> str:
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def _qkey(doc_hash: str, q_norm_hash: str) -> str:
    return f"{doc_hash}:{q_norm_hash}"


def _load_questions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT qid, doc_hash, seq, q_text, a_text, q_norm, q_norm_hash, q_key, simhash "
        "FROM questions ORDER BY qid"
    )
    return [dict(r) for r in rows]


def _plan_questions(rows: list[dict]) -> tuple[list[dict], list[dict], set[int], dict[str, str]]:
    """返回 (updates, drops, drop_qids, old_hash -> new_hash)。"""
    updates: list[dict] = []
    drops: list[dict] = []
    drop_qids: set[int] = set()
    mapping: dict[str, str] = {}
    for r in rows:
        norm = normalize(r["q_text"] or "")
        h = _hash(norm)
        old = r["q_norm_hash"] or ""
        if old:
            mapping.setdefault(old, h)
        updates.append({
            "qid": r["qid"],
            "doc_hash": r["doc_hash"],
            "seq": r["seq"],
            "a_text": r["a_text"],
            "q_norm": norm,
            "q_norm_hash": h,
            "q_key": _qkey(r["doc_hash"], h),
            "simhash": _signed64(simhash(norm)),
            "old_q_norm": r["q_norm"],
            "old_q_norm_hash": old,
            "old_q_key": r["q_key"],
            "old_simhash": r["simhash"],
        })

    # 同一文档、同一新 hash 只保留答案最长的一条；长度相同保留 qid 小的。
    by_doc_hash: dict[tuple[str, str], list[dict]] = {}
    for u in updates:
        by_doc_hash.setdefault((u["doc_hash"], u["q_norm_hash"]), []).append(u)
    for group in by_doc_hash.values():
        if len(group) < 2:
            continue
        keep = sorted(group, key=lambda x: (-len(x.get("a_text") or ""), x["qid"]))[0]
        for u in group:
            if u["qid"] != keep["qid"]:
                drops.append(u)
                drop_qids.add(u["qid"])
    return updates, drops, drop_qids, mapping


def _load_labels(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM label_facts ORDER BY rowid").fetchall()
    except sqlite3.OperationalError:
        # 旧库没有 017 字段时，至少保证 dry-run 不因 schema 未迁移而失败。
        rows = conn.execute(
            "SELECT q_norm_hash, topic, qtype, difficulty, tags, merged_at FROM label_facts "
            "ORDER BY rowid"
        ).fetchall()
    return [dict(r) for r in rows]


def _plan_labels(rows: list[dict], mapping: dict[str, str],
                 q_by_old_hash: dict[str, dict]) -> tuple[list[dict], list[dict], int]:
    """重写标签主键。返回 (新标签, 冲突丢弃, 无法映射保留数)。"""
    planned: list[dict] = []
    dropped: list[dict] = []
    unknown = 0
    groups: dict[str, list[dict]] = {}
    for r in rows:
        old = r.get("q_norm_hash") or ""
        new = mapping.get(old)
        sample = r.get("q_norm_sample") or ""
        if new is None and sample:
            new = _hash(normalize(sample))
        if new is None:
            # 没有快照、也没有对应题目：保留原 hash，但不要伪标成新版本。
            unknown += 1
            new = old
        row = dict(r)
        row["old_q_norm_hash"] = old
        row["_new_q_norm_hash"] = new
        q = q_by_old_hash.get(old)
        if q:
            row["q_norm_sample"] = q["q_norm"]
        row["normalizer_version"] = (
            config.NORMALIZER_VERSION if q or sample else row.get("normalizer_version")
        )
        groups.setdefault(new, []).append(row)

    for new, group in groups.items():
        if len(group) > 1:
            keep = sorted(
                group,
                key=lambda x: (str(x.get("merged_at") or ""), int(x.get("rowid") or 0)),
                reverse=True,
            )[0]
            dropped.extend(x for x in group if x is not keep)
            planned.append(keep)
        else:
            planned.append(group[0])
    return planned, dropped, unknown


def _apply(conn: sqlite3.Connection, plan: dict) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for u in plan["updates"]:
            if u["qid"] in plan["drop_qids"]:
                continue
            # 先把 key 挪到临时命名空间，避免新旧 q_key 在唯一索引上互相撞。
            conn.execute(
                "UPDATE questions SET q_key=? WHERE qid=?",
                (f"__qnorm_mig__:{u['qid']}", u["qid"]),
            )
        if plan["drop_qids"]:
            conn.executemany(
                "DELETE FROM questions WHERE qid=?",
                [(qid,) for qid in sorted(plan["drop_qids"])],
            )
        for u in plan["updates"]:
            if u["qid"] in plan["drop_qids"]:
                continue
            conn.execute(
                "UPDATE questions SET q_norm=?, q_norm_hash=?, q_key=?, simhash=? WHERE qid=?",
                (u["q_norm"], u["q_norm_hash"], u["q_key"], u["simhash"], u["qid"]),
            )

        # 标签表：重写主键，同时保留原有 topic/qtype/difficulty/tags/置信度字段。
        conn.execute("DELETE FROM label_facts")
        for r in plan["labels"]:
            conn.execute(
                "INSERT INTO label_facts "
                "(q_norm_hash, topic, qtype, difficulty, tags, normalizer_version, "
                " q_norm_sample, source, labeled_at, confidence, merged_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("_new_q_norm_hash") or r.get("q_norm_hash"),
                    r.get("topic"),
                    r.get("qtype"),
                    r.get("difficulty"),
                    r.get("tags"),
                    r.get("normalizer_version"),
                    r.get("q_norm_sample"),
                    r.get("source") or "model",
                    r.get("labeled_at"),
                    r.get("confidence"),
                    r.get("merged_at"),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run(db_path: Path, *, apply: bool = False, out: Path | None = None) -> dict:
    db_path = Path(db_path)
    if apply:
        lock = wlock.write_lock(
            "kb",
            tool="migrate_norm",
            build_dir=db_path.parent,
            target=db_path,
        )
    else:
        from contextlib import nullcontext
        lock = nullcontext(None)

    with lock:
        conn = db.connect(db_path, readonly=not apply)
        try:
            questions = _load_questions(conn)
            updates, drops, drop_qids, mapping = _plan_questions(questions)
            # 供 label_facts 回填 q_norm_sample：old hash 对应一条代表题。
            q_by_old_hash: dict[str, dict] = {}
            for u in updates:
                q_by_old_hash.setdefault(u["old_q_norm_hash"], u)
            label_rows = _load_labels(conn)
            labels, label_drops, label_unknown = _plan_labels(
                label_rows, mapping, q_by_old_hash,
            )
            labels_total = len(label_rows)
            changed = sum(
                1 for u in updates
                if u["old_q_norm"] != u["q_norm"]
                or u["old_q_norm_hash"] != u["q_norm_hash"]
                or u["old_q_key"] != u["q_key"]
                or u["old_simhash"] != u["simhash"]
            )
            plan = {
                "db": str(db_path),
                "generated_at": datetime.now().strftime(NOW_FMT),
                "normalizer_version": config.NORMALIZER_VERSION,
                "dry_run": not apply,
                "questions_total": len(questions),
                "questions_changed": changed,
                "questions_dropped": len(drops),
                "drop_qids": sorted(drop_qids),
                "dropped_pairs": [
                    {"qid": d["qid"], "doc_hash": d["doc_hash"], "new_hash": d["q_norm_hash"]}
                    for d in drops
                ],
                "labels_total": labels_total,
                "labels_rewritten": len(labels),
                "labels_dropped": len(label_drops),
                "labels_unmapped": label_unknown,
                "old_to_new": dict(sorted(mapping.items())),
                "updates": updates,
                "labels": labels,
                "drop_qids_set": drop_qids,
            }
            if apply:
                _apply(conn, plan)
            summary = {
                k: plan[k] for k in (
                    "db", "generated_at", "normalizer_version", "dry_run",
                    "questions_total", "questions_changed", "questions_dropped",
                    "labels_total", "labels_rewritten", "labels_dropped",
                    "labels_unmapped",
                )
            }
            summary["old_to_new_count"] = len(plan["old_to_new"])
            summary["drop_qids"] = plan["drop_qids"]
            summary["dropped_pairs"] = plan["dropped_pairs"]
            summary["requires_canonical_rebuild"] = True
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                snapshot = dict(summary)
                # 迁移快照必须能复核每个旧 hash 的新去向；只存计数无法在回滚/审计时使用。
                snapshot["old_to_new"] = plan["old_to_new"]
                out.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            return summary
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="q_norm_hash 归一器迁移（dry-run 默认）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只出快照）")
    ap.add_argument("--out", default="", help="快照 JSON 路径；默认 <db 目录>/qnorm_migration.json")
    args = ap.parse_args(argv)
    db_path = Path(args.db)
    out = Path(args.out) if args.out else db_path.parent / "qnorm_migration.json"
    result = run(db_path, apply=args.apply, out=out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
