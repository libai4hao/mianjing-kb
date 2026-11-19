# -*- coding: utf-8 -*-
"""preflight：生产迁移前的只读基线核对。

只做三件事：
1. 用 SQLite `mode=ro` 打开目标库，绝不执行迁移、绝不写库；
2. 输出 schema_version / 待应用迁移 / integrity / 关键计数；
3. 给 qnorm 与 016 的可核验证据，不根据 docs 快照猜生产状态。

用法：
    kb preflight --db "<CORPUS_ROOT>/21-面经数据库/build/kb.sqlite" --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mianjing_kb.kb import config, db  # noqa: E402


def _ro_uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def collect(db_path: Path) -> dict:
    if not db_path.is_file():
        return {"db": str(db_path), "exists": False, "error": "数据库文件不存在"}
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out: dict = {"db": str(db_path), "exists": True}
        out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        out["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        out["schema_version"] = (
            conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if _table_exists(conn, "schema_version") else None
        )
        applied = db.applied_versions(conn) if _table_exists(conn, "schema_version") else []
        known = [v for v, _fn in db.MIGRATIONS]
        out["applied_versions"] = applied
        out["pending_migrations"] = [v for v in known if v not in applied]
        out["tables"] = {
            "source_documents": _count(conn, "source_documents"),
            "document_products": _count(conn, "document_products"),
            "questions": _count(conn, "questions"),
            "canonical_questions": _count(conn, "canonical_questions"),
            "labels_staging": _count(conn, "labels_staging"),
            "label_facts": _count(conn, "label_facts"),
        }
        qcols = _columns(conn, "questions")
        out["questions_columns"] = {
            "q_key": "q_key" in qcols,
            "q_norm_hash": "q_norm_hash" in qcols,
            "q_norm": "q_norm" in qcols,
        }
        out["qnorm"] = {"normalizer_versions": [], "evidence": "no label_facts"}
        if _table_exists(conn, "label_facts") and "normalizer_version" in _columns(conn, "label_facts"):
            rows = conn.execute(
                "SELECT normalizer_version, COUNT(*) FROM label_facts "
                "GROUP BY normalizer_version ORDER BY normalizer_version"
            ).fetchall()
            out["qnorm"] = {
                "normalizer_versions": [
                    {"version": r[0], "rows": r[1]} for r in rows
                ],
                "evidence": "label_facts.normalizer_version",
            }
        out["warnings"] = []
        if out["pending_migrations"]:
            out["warnings"].append(
                "存在待应用迁移；不要用 db.connect(create=True) 打开生产库，"
                "先按 D-27 做备份/预演/验收"
            )
        if out["integrity_check"] != "ok" or out["quick_check"] != "ok":
            out["warnings"].append("完整性检查未通过，禁止迁移")
        return out
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读核对生产库迁移基线")
    ap.add_argument("--db", default=str(Path(config.DB_PATH)), help="kb.sqlite 路径")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    data = collect(Path(args.db))
    print(json.dumps(data, ensure_ascii=False, indent=1))
    if not data.get("exists"):
        return 2
    return 0 if data.get("integrity_check") == "ok" and data.get("quick_check") == "ok" else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
