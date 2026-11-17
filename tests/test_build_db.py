# -*- coding: utf-8 -*-
"""P2-④ build_db 测试：stem 剥壳 + 查询路由（LIKE 兜底 / FTS trigram）+ 镜像题 topic 继承链。"""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build_db import _base_stem, demo_query  # noqa: E402


def test_base_stem():
    assert _base_stem("面试题") == "面试题"
    assert _base_stem("面试题_a1b2c3d4") == "面试题"
    assert _base_stem("面试题__a1b2c3d4") == "面试题"    # backfill 双下划线口径（审核修复：先判两位）
    assert _base_stem("ab_cdef123") == "ab_cdef123"          # 后缀不足 8 位十六进制
    assert _base_stem("面试题_12345678") == "面试题"           # 纯数字 8 位也在 hex 空间，会被剥掉（可接受）
    assert _base_stem("ab") == "ab"
    assert _base_stem("ab__cdef1234") == "ab"


@pytest.fixture()
def mini_db(tmp_path):
    """手工建最小检索库：2 题 + FTS，验证两条查询路径。"""
    p = tmp_path / "mini.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('h1', 'Java面试题.pdf');
INSERT INTO questions VALUES(1, 'h1', 'HashMap的扩容机制是什么？', '扩容发生在put时…', '八股', 'Java基础', '题库');
INSERT INTO questions VALUES(2, 'h1', 'Tomcat缺省端口是多少？', '缺省端口是8080。', '八股', '中间件', '题库');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(1, 'HashMap的扩容机制是什么？', '扩容发生在put时…');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(2, 'Tomcat缺省端口是多少？', '缺省端口是8080。');
""")
    con.commit()
    con.close()
    return p


def test_query_like_fallback_2char(mini_db):
    hits = demo_query(mini_db, "端口", 5)
    assert len(hits) == 1
    assert hits[0]["q_text"].startswith("Tomcat")
    assert hits[0]["file_name"] == "Java面试题.pdf"


def test_query_fts_trigram(mini_db):
    hits = demo_query(mini_db, "HashMap", 5)
    assert len(hits) == 1 and "扩容" in hits[0]["q_text"]
    assert demo_query(mini_db, "不存在的词", 5) == []


def test_mirror_topic_inheritance(tmp_path, monkeypatch):
    """G3/K12 修复：镜像题（无台账行）topic 落空串 → 继承链 源文档 topic → md_rel 目录段。"""
    from tools import build_db as bdb
    from tools.kb import config, db as kbdb
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    md_dir = corpus / "20-题库MD" / "题库" / "JVM"
    build.mkdir(parents=True)
    md_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(bdb, "MD_ROOT", corpus / "20-题库MD")

    mirror_rel = "题库/JVM/甲.md"
    mirror_h = hashlib.sha256(("md-mirror:" + mirror_rel).encode()).hexdigest()[:16]
    kb = kbdb.connect(build / "kb.sqlite")
    kb.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, category) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("h1", "h1" + "0" * 48, "甲.pdf", ".pdf", 1, "x/甲.pdf", "02", "classified", "t",
         "now", "now", "JVM", "题库"))
    kb.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at) VALUES "
        "(1, 'h1', 1, '什么是JVM？', '', '八股', 'high', '什么是jvm', 'x', 0, 0, 1, 'now')")
    kb.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at) VALUES "
        "(2, ?, 1, '什么是类加载？', '', '八股', 'high', '什么是类加载', 'y', 0, 0, 1, 'now')",
        (mirror_h,))
    kb.commit()
    kb.close()
    (md_dir / "甲.md").write_text("# 甲\n\n什么是JVM？", encoding="utf-8")

    out_db = build / "面经库.db"
    bdb.build(out_db=out_db)
    con = sqlite3.connect(str(out_db))
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT qid, topic, category FROM questions")}
    con.close()
    assert rows[1] == ("JVM", "题库")      # 源题：走台账 topic（不变）
    assert rows[2] == ("JVM", "题库")      # 镜像题：无台账行 → md_rel 目录段继承，不再空串
