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


def test_008_golden_labels_migration(tmp_path):
    """008：golden_labels 金标表（批次二 G1，键=q_norm_hash 跨重切存活）。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "golden_labels" in tables
    assert _cols(conn, "golden_labels") >= {
        "q_norm_hash", "q_text", "topic_gold", "qtype_gold",
        "difficulty_gold", "annotator", "created_at"}
    # annotator 默认 human
    conn.execute(
        "INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
        "VALUES ('h1', '题干', 'JVM', '2026-11-19T00:00:00')")
    r = conn.execute("SELECT annotator FROM golden_labels WHERE q_norm_hash='h1'").fetchone()
    assert r[0] == "human"
    conn.close()


def test_009_review_columns_migration(tmp_path):
    """009：labels_staging 加复核三列（批次三 R1）。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    assert {"review_state", "review_model", "review_note"} <= _cols(conn, "labels_staging")
    # NULL = 未复核（不加 NOT NULL 默认，历史行为兼容）
    conn.execute(
        "INSERT INTO labels_staging(qid, topic, labeled_at) VALUES (1, 'JVM', 't')")
    r = conn.execute("SELECT review_state FROM labels_staging WHERE qid=1").fetchone()
    assert r[0] is None
    conn.close()
