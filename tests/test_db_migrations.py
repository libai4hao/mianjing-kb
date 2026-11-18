# -*- coding: utf-8 -*-
"""db.migrate 回归：007 label_facts 打标事实源 + staging/questions 伴随列（B1 修复）。"""
from tools.kb import db


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_007_label_facts_migration(tmp_path):
    """007：label_facts 表 + labels_staging.q_norm_hash + questions.difficulty/tags。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "label_facts" in tables
    assert _cols(conn, "label_facts") >= {
        "q_norm_hash", "topic", "qtype", "difficulty", "tags", "merged_at"}
    assert "q_norm_hash" in _cols(conn, "labels_staging")
    assert {"prompt_tokens", "completion_tokens"} <= _cols(conn, "labels_staging")
    assert {"difficulty", "tags"} <= _cols(conn, "questions")
    conn.close()


def test_migration_idempotent(tmp_path):
    """二次连接重跑迁移不报错（幂等：IF NOT EXISTS + PRAGMA 探列）。"""
    p = tmp_path / "kb.sqlite"
    db.connect(p).close()
    conn2 = db.connect(p)
    assert conn2.execute("SELECT COUNT(*) FROM label_facts").fetchone()[0] == 0
    conn2.close()
