# -*- coding: utf-8 -*-
"""D-37 集成层：真实 BGE 模型 + 小型检索库 top-k。仅在 RUN_BGE_INTEGRATION=1 时跑。"""
import os
import sqlite3

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastembed")
pytest.importorskip("sqlite_vec")

import sqlite_vec  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402

from mianjing_kb import embed_questions as E  # noqa: E402
from mianjing_kb import search as S  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    if os.environ.get("RUN_BGE_INTEGRATION") != "1":
        pytest.skip("RUN_BGE_INTEGRATION!=1，跳过真实模型检索集成测试")
    path = tmp_path / "kb.db"
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(
        "CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);"
        "CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, "
        "mirror_hash TEXT, content TEXT);"
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, "
        "a_text TEXT, qtype TEXT, topic TEXT, difficulty INTEGER, tags_json TEXT, "
        "position TEXT, quality TEXT, q_key TEXT, q_norm_hash TEXT);")
    rows = [
        (1, "MySQL 索引为什么用 B+ 树", "B+ 树叶子链表适合范围查询。", "MySQL", "d1", "n1"),
        (2, "Redis 持久化 RDB 和 AOF 的区别", "RDB 快照，AOF 日志。", "Redis", "d2", "n2"),
        (3, "Java 线程池核心参数", "核心线程、队列、拒绝策略。", "Java基础", "d3", "n3"),
    ]
    for qid, text, answer, topic, doc, norm in rows:
        con.execute("INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, "
                    "difficulty, q_key, q_norm_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (qid, doc, text, answer, "八股", topic, 3, f"{doc}:{norm}", norm))
    E.create_vec_table(con)
    model = TextEmbedding(E.MODEL)
    vecs = list(model.embed([r[1] for r in rows]))
    for row, vec in zip(rows, vecs):
        qid, _text, _answer, topic, doc, norm = row
        con.execute(
            "INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty, q_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (qid, sqlite_vec.serialize_float32(vec), topic, "八股", 3, f"{doc}:{norm}"))
    E.write_vec_meta(con)
    con.commit()
    E._MODEL = model
    yield con
    con.close()


def test_real_retrieval_top_k_and_vec_meta(db):
    hits = S.vector_search(db, "MySQL 索引为什么用 B+ 树", 3)
    assert hits and hits[0]["qid"] == 1
    meta = E.read_vec_meta(db)
    assert meta["model"] == E.MODEL and meta["dim"] == str(E.DIM)
    assert meta["question_set_hash"] == E.question_set_hash(db)


def test_real_retrieval_dimension_contract(db):
    hits = S.vector_search(db, "Redis 持久化", 3)
    assert hits[0]["qid"] == 2
    assert len(hits) <= 3
