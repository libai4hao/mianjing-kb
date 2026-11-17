# -*- coding: utf-8 -*-
"""P2-④ build_db 轻量测试：stem 剥壳 + 查询路由（LIKE 兜底 / FTS trigram）。"""
import sqlite3

import pytest

from tools.build_db import _base_stem, demo_query


def test_base_stem():
    assert _base_stem("面试题") == "面试题"
    assert _base_stem("面试题_a1b2c3d4") == "面试题"
    assert _base_stem("ab_cdef123") == "ab_cdef123"          # 后缀不足 8 位十六进制
    assert _base_stem("面试题_12345678") == "面试题"           # 纯数字 8 位也在 hex 空间，会被剥掉（可接受）
    assert _base_stem("ab") == "ab"


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
