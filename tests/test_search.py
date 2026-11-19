# -*- coding: utf-8 -*-
"""P3-① search 测试：RRF 融合 + kw 双路由 + hybrid 降级。"""
import sqlite3

import pytest

from mianjing_kb import search as S
from mianjing_kb.search import has_vec_table, hybrid_search, keyword_search, rag_contexts, rrf_fuse

MIRROR_REL = "题库/综合/手写题.md"
MIRROR_HASH = __import__("hashlib").sha256(("md-mirror:" + MIRROR_REL).encode()).hexdigest()[:16]


@pytest.fixture()
def mini_db(tmp_path):
    p = tmp_path / "mini.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript(f"""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER,
                       tags_json TEXT, position TEXT, quality TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT, PRIMARY KEY(qid, tag_norm));
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('h1', 'MySQL面试题.pdf');
INSERT INTO doc_contents(md_rel, doc_hash, mirror_hash, content) VALUES('{MIRROR_REL}', NULL, '{MIRROR_HASH}', 'x');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, category, difficulty, tags_json, position, quality) VALUES
(1, 'h1', 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。', '八股', 'MySQL', '题库', 3, '["B+树","索引"]', '["后端"]', NULL),
(2, 'h1', 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。', '八股', 'MySQL', '题库', 4, '["复制"]', '["后端","数据"]', '["重复"]'),
(3, 'h1', 'Redis持久化有哪几种方式？', 'RDB 与 AOF。', '八股', 'Redis', '题库', 2, '["持久化"]', '["后端"]', NULL),
(4, '{MIRROR_HASH}', '手写LRU缓存如何实现？', '哈希表加双向链表。', '算法', '数据结构', '镜像', 5, '["LRU"]', '["算法"]', NULL);
INSERT INTO question_tags(qid, tag, tag_norm) VALUES
(1,'B+树','b+树'),(1,'索引','索引'),(2,'复制','复制'),(3,'持久化','持久化'),(4,'LRU','lru');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(1, 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(2, 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(3, 'Redis持久化有哪几种方式？', 'RDB 与 AOF。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(4, '手写LRU缓存如何实现？', '哈希表加双向链表。');
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


def test_source_resolution(mini_db):
    """P3-③：documents 命中给 file_name；md 镜像题回链 doc_contents.md_rel。"""
    hits = keyword_search(mini_db, "主从复制", 5)
    assert hits[0]["source"] == "MySQL面试题.pdf"
    hits = keyword_search(mini_db, "手写LRU", 5)
    assert len(hits) == 1 and hits[0]["source"] == MIRROR_REL


def test_rag_contexts(mini_db):
    out = rag_contexts(mini_db, "手写LRU", 3)
    ctx = out["contexts"]
    assert out["query"] == "手写LRU"
    assert len(ctx) == 1
    assert ctx[0]["answer"].startswith("哈希表加双向链表")
    assert ctx[0]["source"] == MIRROR_REL


def test_keyword_filters_difficulty_position_quality_tags(mini_db):
    hits = keyword_search(mini_db, "主从复制", 10, difficulty=4, position="后端",
                          quality="重复", tags=["复制"])
    assert len(hits) == 1 and hits[0]["qid"] == 2
    assert hits[0]["difficulty"] == 4 and hits[0]["tags_json"] == '["复制"]'


def test_keyword_tags_are_and(mini_db):
    assert keyword_search(mini_db, "InnoDB", 10, tags=["B+树", "索引"])[0]["qid"] == 1
    assert keyword_search(mini_db, "InnoDB", 10, tags=["B+树", "复制"]) == []


def test_vector_filter_pushed_into_knn(tmp_path, monkeypatch):
    """T1 核心：B 类向量更近，topic=A 过滤必须在 KNN 内完成，否则后过滤会 0 条。"""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from mianjing_kb import embed_questions as E

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT, "
        "qtype TEXT, topic TEXT, difficulty INTEGER, tags_json TEXT, position TEXT, quality TEXT);"
        "CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);"
        "CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, "
        "mirror_hash TEXT, content TEXT);"
        "CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);")
    E.create_vec_table(con)

    def vec(first: float, second: float) -> list[float]:
        vals = [0.0] * E.DIM
        vals[0], vals[1] = first, second
        return vals

    # 10 条 B 更靠近查询，10 条 A 更远：后过滤会拿不到 A，metadata 下推才正确。
    for i in range(1, 21):
        topic = "B" if i <= 10 else "A"
        qid = i
        con.execute("INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty) "
                    "VALUES (?, 'h', ?, '', '八股', ?, 3)", (qid, f"题{qid}", topic))
        near = 0.01 if topic == "B" else 0.5 + i / 1000
        con.execute("INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty) "
                    "VALUES (?,?,?,?,?)",
                    (qid, sqlite_vec.serialize_float32(vec(1.0, near)), topic, "八股", 3))
    con.commit()

    monkeypatch.setattr(E, "embed_texts", lambda texts: [vec(1.0, 0.0) for _ in texts])
    rows = S.vector_search(con, "任意", 5, topic="A")
    assert len(rows) == 5
    assert {r["topic"] for r in rows} == {"A"}
    con.close()

