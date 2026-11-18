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


def test_schema_version_records_all_migrations(tmp_path):
    """统一迁移口径：每次落地的版本都进 schema_version，006 也在册（不再"找不到迁移文件"）。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    versions = db.applied_versions(conn)
    assert versions == [v for v, _fn in db.MIGRATIONS]
    assert "006_questions_topic" in versions
    # 重复跑不重复记账
    db.migrate(conn)
    assert db.applied_versions(conn) == versions
    conn.close()


def test_legacy_db_without_schema_version_is_backfilled(tmp_path):
    """老库（有表但没有 schema_version）首次连接后应补齐版本记录，且不报错。"""
    import sqlite3
    p = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(str(p))
    raw.executescript(db.SCHEMA_FILE.read_text(encoding="utf-8"))
    raw.execute("DROP TABLE IF EXISTS schema_version")      # 模拟改造前的老库
    raw.commit()
    raw.close()
    conn = db.connect(p)
    assert db.applied_versions(conn) == [v for v, _fn in db.MIGRATIONS]
    conn.close()


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


def test_connect_create_false_does_not_create_file(tmp_path):
    """评审 2026-11-19：--db 写错时不能悄悄生成空库（sqlite3.connect 默认会建）。"""
    import pytest
    missing = tmp_path / "typo.sqlite"
    with pytest.raises(FileNotFoundError):
        db.connect(missing, create=False)
    assert not missing.exists()


def test_upsert_document_does_not_reset_existing_record(tmp_path):
    """已有记录再 upsert 默认 DO NOTHING：status / first_seen_at 不能被覆盖。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    row = {"doc_hash": "h1", "sha256": "x" * 64, "file_name": "a.pdf", "src_path": "/a.pdf",
           "dir_path": "", "batch_id": "b1", "first_seen_at": "2026-11-01T00:00:00",
           "last_seen_at": "2026-11-01T00:00:00", "status": "classified"}
    assert db.upsert_document(conn, row) == "inserted"
    conn.execute("UPDATE source_documents SET status='classified', first_seen_at='2026-11-01T00:00:00'")
    row2 = dict(row, status="pending_classify", first_seen_at="2026-11-19T00:00:00")
    assert db.upsert_document(conn, row2) == "exists"
    r = conn.execute("SELECT status, first_seen_at FROM source_documents WHERE doc_hash='h1'").fetchone()
    assert r["status"] == "classified" and r["first_seen_at"] == "2026-11-01T00:00:00"
    conn.close()


def test_record_batch_conflict_raises_instead_of_overwrite(tmp_path):
    """同一 batch_id 第二次写必须报错，不能静默覆盖上一批统计。"""
    import sqlite3
    import pytest
    conn = db.connect(tmp_path / "kb.sqlite")
    kw = dict(batch_id="b1", created_at="t", root="r", mode="full",
              added=1, duplicated=0, moved=0, quarantined=0, ignored=0, note="")
    db.record_batch(conn, **kw)
    with pytest.raises(sqlite3.IntegrityError):
        db.record_batch(conn, **dict(kw, added=99))
    conn.close()


def test_backup_db_includes_uncheckpointed_wal(tmp_path):
    """backup API 必须带上 WAL 里已提交未 checkpoint 的数据（copy2 会漏）。"""
    conn = db.connect(tmp_path / "kb.sqlite")
    conn.execute("INSERT INTO source_documents(doc_hash, sha256, file_name, src_path, "
                 "batch_id, first_seen_at, last_seen_at, status, dir_path) "
                 "VALUES ('h1','s','a.pdf','/a.pdf','b','t','t','pending_classify','')")
    conn.commit()
    backup = db.backup_db(tmp_path / "kb.sqlite", tmp_path / "backups", keep=2)
    b = db.connect(backup, create=False)
    assert b.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
    b.close()
    conn.close()


def test_fresh_and_upgraded_schema_identical(tmp_path):
    """全新库 vs 从 001~008 增量升级的库，sqlite_master 必须完全一致。"""
    import sqlite3
    fresh = db.connect(tmp_path / "fresh.sqlite")
    legacy_path = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(str(legacy_path))
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, fn in db.MIGRATIONS[:8]:
        fn(raw)
        raw.execute("INSERT INTO schema_version VALUES (?, 't')", (version,))
    raw.commit()
    raw.close()
    upgraded = db.connect(legacy_path)

    sql = ("SELECT type, name, sql FROM sqlite_master "
           "WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_version' "
           "ORDER BY type, name")
    assert fresh.execute(sql).fetchall() == upgraded.execute(sql).fetchall()
    fresh.close()
    upgraded.close()


def test_009_partial_state_is_completed(tmp_path):
    """009 中途失败留下的半完成状态（只加了 review_state）能被补齐。"""
    import sqlite3
    p = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(str(p))
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, fn in db.MIGRATIONS[:8]:
        fn(raw)
        raw.execute("INSERT INTO schema_version VALUES (?, 't')", (version,))
    raw.execute("ALTER TABLE labels_staging ADD COLUMN review_state TEXT")  # 第一条 ALTER 成功
    raw.commit()
    raw.close()
    conn = db.connect(p)
    assert {"review_state", "review_model", "review_note"} <= _cols(conn, "labels_staging")
    conn.close()
