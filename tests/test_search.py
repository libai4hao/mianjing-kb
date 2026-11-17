# -*- coding: utf-8 -*-
"""P3-① search 测试：RRF 融合 + kw 双路由 + hybrid 降级。"""
import sqlite3

import pytest

from tools.search import has_vec_table, hybrid_search, keyword_search, rrf_fuse


@pytest.fixture()
def mini_db(tmp_path):
    p = tmp_path / "mini.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('h1', 'MySQL面试题.pdf');
INSERT INTO questions VALUES(1, 'h1', 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。', '八股', 'MySQL', '题库');
INSERT INTO questions VALUES(2, 'h1', 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。', '八股', 'MySQL', '题库');
INSERT INTO questions VALUES(3, 'h1', 'Redis持久化有哪几种方式？', 'RDB 与 AOF。', '八股', 'Redis', '题库');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(1, 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(2, 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(3, 'Redis持久化有哪几种方式？', 'RDB 与 AOF。');
""")
    yield con
    con.close()


def test_rrf_fuse():
    fused = rrf_fuse([1, 2, 3], [3, 1])
    assert fused[0][0] == 1            # 两路都靠前 → 融合第一
    assert fused[0][1] > fused[1][1]
    assert len(fused) == 3
    assert rrf_fuse([], [5])[0] == (5, 1 / 61)


def test_kw_search_routes(mini_db):
    hits = keyword_search(mini_db, "主从复制", 10)          # ≥3 字走 FTS
    assert len(hits) == 1 and hits[0]["qid"] == 2
    hits = keyword_search(mini_db, "B树", 10)               # 2 字走 LIKE
    assert len(hits) == 1 and hits[0]["qid"] == 1
    hits = keyword_search(mini_db, "主从", 10, topic="MySQL")
    assert len(hits) == 1 and hits[0]["topic"] == "MySQL"


def test_hybrid_degrades_to_kw(mini_db):
    assert has_vec_table(mini_db) is False
    hits = hybrid_search(mini_db, "主从复制", 5)
    assert len(hits) == 1 and "rrf" not in hits[0]
