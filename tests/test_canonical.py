# -*- coding: utf-8 -*-
"""canonical 落库测试：簇→两表映射、topic 多数票、witness、幂等重建、--dry-run。"""
import sqlite3

import pytest
from mianjing_kb import canonical as cn  # noqa: E402
from mianjing_kb.kb import config, db as kbdb  # noqa: E402

REAL_META = config.META_DIR

N_JVM = "什么是jvm内存模型"          # 归一化后（normalize 去标点小写）
N_MYSQL = "什么是mysql索引"
N_REDIS = "redis持久化机制"


@pytest.fixture()
def mini_db(tmp_path, monkeypatch):
    """kb.sqlite：3 簇（JVM×2docs / MySQL×3docs / Redis 单件）+ 对应 source_documents。"""
    build = tmp_path / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    conn = sqlite3.connect(build / "kb.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript((REAL_META / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    conn.executescript((REAL_META / "schema" / "004_questions.sql").read_text(encoding="utf-8"))
    docs = [("h1", "甲.pdf", "JVM", "c1"), ("h2", "乙.pdf", "JVM", "c2"),
            ("h3", "丙.pdf", "MySQL", "c3"), ("h4", "丁.pdf", "MySQL", "c4"),
            ("h5", "戊.pdf", "Redis", "c5")]
    for h, name, topic, col in docs:
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, collection) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, h + "0" * 48, name, ".pdf", 1, f"x/{name}", "02", "classified", "t",
             "now", "now", topic, col))
    # 5 道题：X=JVM 簇(甲,乙)；Y=MySQL 簇(乙,丙,丁)；Z=Redis 单件(戊)
    rows = [
        ("h1", 1, N_JVM), ("h2", 1, N_JVM),
        ("h2", 2, N_MYSQL), ("h3", 1, N_MYSQL), ("h4", 1, N_MYSQL),
        ("h5", 1, N_REDIS),
    ]
    for i, (h, seq, norm) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
            "q_norm_hash, simhash, span_start, span_end, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, h, seq, norm, f"答案{i}", "八股", "high", norm, norm[:16], 0, 0, 1, "now"))
    kbdb._ensure_stable_question_keys(conn)   # T29：测试库补 q_key/依赖表（与正式迁移同函数）
    conn.commit()
    yield conn
    conn.close()


def test_build_and_counts(mini_db):
    assert cn.main([]) == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    # 6 变体 = X 簇 2（甲,乙）+ Y 簇 3（乙,丙,丁）+ Z 单件 1（戊）
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6


def test_witness_and_topic_vote(mini_db):
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 2 and rows[N_JVM]["topic"] == "JVM"
    assert rows[N_MYSQL]["witness"] == 3 and rows[N_MYSQL]["topic"] == "MySQL"
    assert rows[N_REDIS]["witness"] == 1 and rows[N_REDIS]["topic"] == "Redis"
    # variant 的 collection 归属与 canonical_id 级联
    vid = cn.variant_id("h1", 1)
    var = mini_db.execute("SELECT * FROM question_variants WHERE variant_id=?", (vid,)).fetchone()
    assert var["collection"] == "c1"
    assert var["canonical_id"] == rows[N_JVM]["canonical_id"]


def test_witness_uses_distinct_collection(mini_db):
    """Q-2：见证数 = distinct collection——同 collection 的多篇只算 1 个来源。"""
    mini_db.execute("UPDATE source_documents SET collection='c1' WHERE doc_hash IN ('h1','h2')")
    mini_db.execute("UPDATE source_documents SET collection='c9' WHERE doc_hash IN ('h3','h4')")
    mini_db.commit()
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 1          # 甲、乙同 c1 → 1 个来源
    assert rows[N_MYSQL]["witness"] == 2        # 乙(c1) + 丙/丁(c9) → 2 个来源


def test_witness_falls_back_to_doc_hash_when_collection_empty(mini_db):
    mini_db.execute("UPDATE source_documents SET collection=NULL")
    mini_db.commit()
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 2          # 空 collection 不能合成 1 个


def test_idempotent_and_dry_run(mini_db):
    cn.main([])
    cn.main([])                                    # 重建幂等：不翻倍
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6
    # dry-run 不写
    mini_db.execute("DELETE FROM canonical_questions")
    mini_db.execute("DELETE FROM question_variants")
    mini_db.commit()
    cn.main(["--dry-run"])
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 0


def test_incremental_attach_create_and_manifest_rollback(mini_db, tmp_path):
    """T29：同题吸附、新题建簇，都只写增量；manifest apply 回滚到原状。"""
    from mianjing_kb import manifests
    cn.main([])
    # 新增两题：h6 与 JVM 同 norm（吸附），h7 全新（建簇）
    mini_db.executemany(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, collection) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("h6", "h6" + "0" * 48, "己.pdf", ".pdf", 1, "x/己.pdf", "02", "classified", "t",
          "now", "now", "JVM", "c6"),
         ("h7", "h7" + "0" * 48, "庚.pdf", ".pdf", 1, "x/庚.pdf", "02", "classified", "t",
          "now", "now", "JVM", "c7")])
    mini_db.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at, q_key) "
        "VALUES (7,'h6',1,?,'答案7','八股','high',?,?,0,0,1,'now',?)",
        (N_JVM, N_JVM, N_JVM[:16], f"h6:{N_JVM[:16]}"))
    mini_db.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at, q_key) "
        "VALUES (8,'h7',1,?,'答案8','八股','high',?,?,0,0,1,'now',?)",
        ("什么是垃圾回收", "什么是垃圾回收", "gc" * 8, "h7:" + "gc" * 8))
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t1"]) == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 4
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 8
    jvm = mini_db.execute(
        "SELECT canonical_id,witness FROM canonical_questions WHERE title_norm=?", (N_JVM,)).fetchone()
    assert jvm["witness"] == 3                     # c1/c2/c6 三个 collection
    mf = manifests.load("t1", tmp_path / "build")
    assert mf and mf["status"] == "committed"
    assert len(mf["created_canonicals"]) == 1 and len(mf["attached"]) == 1

    out = manifests.rollback(mini_db, "t1", apply=True, build_dir=tmp_path / "build")
    assert out["ok"] and out["status"] == "rolled_back"
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6
