# -*- coding: utf-8 -*-
"""存量题库的题干显示清洗（F4）：只改 q_text，不动 q_norm / q_key。

为什么单独做一个命令，而不是“重跑 extract”：
`extract` 会按当前 chunker 全量重切，切题规则一旦迭代（11-22 的 TOC/续行/列表修复），
题数与 `q_norm_hash` 都会变，牵连标签、向量、canonical。生产 1455 份文档抽样重切，
25 篇里 16 篇题量变化（`1_Java面试宝典` 97→588），那是独立的数据迁移，不是“洗个星号”。
本命令只做显示层清洗：`q_text = clean_question_text(q_text)`，`q_norm`/`q_key` 原样，
所以标签、向量、簇都不需要重算；跑完刷新检索库（build_db 会重建 FTS）即可生效。

用法：
    python -m mianjing_kb.clean_qtext --db build/kb.sqlite            # 只报告（默认 dry-run）
    python -m mianjing_kb.clean_qtext --db build/kb.sqlite --apply    # 真正写库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db as kbdb, wlock  # noqa: E402
from mianjing_kb.kb.qtext import clean_question_text  # noqa: E402

LOCK_WAIT_SEC = 30.0


def clean_rows(conn, apply: bool, samples: int = 10) -> dict:
    scanned = changed = 0
    examples: list[dict] = []
    updates: list[tuple[str, int]] = []
    for row in conn.execute("SELECT qid, q_text FROM questions"):
        scanned += 1
        old = row["q_text"] or ""
        new = clean_question_text(old)
        if new == old.strip():
            continue
        changed += 1
        if len(examples) < samples:
            examples.append({"qid": row["qid"], "before": old[:90], "after": new[:90]})
        updates.append((new[:2000], row["qid"]))
    if apply and updates:
        conn.executemany("UPDATE questions SET q_text=? WHERE qid=?", updates)
        conn.commit()
    return {"scanned": scanned, "changed": changed, "applied": bool(apply),
            "examples": examples}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="存量题库 q_text 显示清洗（F1 身份不动）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只报告")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(json.dumps({"error": f"库不存在：{path}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    build_dir = Path(config.BUILD_DIR)
    lock = wlock.write_lock("kb", tool="clean_qtext", build_dir=build_dir,
                            wait_sec=LOCK_WAIT_SEC, target=path)
    try:
        lock.__enter__()
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        conn = kbdb.connect(path, create=False, readonly=not args.apply)
        conn.row_factory = sqlite3.Row
        try:
            out = clean_rows(conn, args.apply)
        finally:
            conn.close()
    finally:
        lock.__exit__(None, None, None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
