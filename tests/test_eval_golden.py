# -*- coding: utf-8 -*-
"""D-34：Golden Query 数据集结构与指标口径。"""
import sqlite3
from pathlib import Path

import pytest

from mianjing_kb import eval_retrieval as R

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden_queries.yaml"


def test_golden_dataset_shape_and_honest_review_status():
    rows = R.load_queries(GOLDEN)
    assert len(rows) == 50
    counts = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
        assert row["reviewed"] is False          # 首版候选尚未人工审核，不得伪称已审
        assert isinstance(row["expect_q_keys"], list)
        assert isinstance(row["relevance"], list)
        if row["category"] != "no_answer":
            assert row["expect_q_keys"]
            assert {x["q_key"] for x in row["relevance"]} == set(row["expect_q_keys"])
    assert counts == {"exact": 10, "paraphrase": 10, "multi_keyword": 10,
                      "short": 8, "filtered": 6, "no_answer": 6}


def test_ndcg_prefers_higher_grade_and_higher_rank():
    rel = {"a": 3.0, "b": 2.0}
    assert R._ndcg(["a", "b"], rel, 10) == pytest.approx(1.0)
    assert R._ndcg(["b", "a"], rel, 10) < 1.0
    assert R._ndcg([], rel, 10) == 0.0


def _mini_db(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER,
                       tags_json TEXT, position TEXT, quality TEXT, q_key TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('d1', 'MySQL.pdf');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key)
VALUES (1, 'd1', 'MySQL 索引为什么用 B+ 树', '叶子节点链表。', '八股', 'MySQL', 3, 'd1:q1');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES (1, 'MySQL 索引为什么用 B+ 树', '叶子节点链表。');
""")
    con.commit()
    con.close()


def test_evaluate_golden_counts_hits_and_no_answer_false_positive(tmp_path):
    db = tmp_path / "mini.db"
    _mini_db(db)
    queries = [
        {"query": "MySQL 索引", "topic": "MySQL", "category": "exact",
         "expect_q_keys": ["d1:q1"], "relevance": [{"q_key": "d1:q1", "grade": 3}],
         "reviewed": False},
        {"query": "唐宋八大家", "topic": None, "category": "no_answer",
         "expect_q_keys": [], "relevance": [], "reviewed": False},
    ]
    out = R.evaluate_golden(db, queries, k=10, mode="kw")
    assert out["recall_at_5"] == 1.0 and out["mrr_at_10"] == 1.0
    assert out["no_answer_false_positive_rate"] == 0.0
    assert out["per_topic_recall_at_5"]["MySQL"]["recall_at_5"] == 1.0


def test_reviewed_requires_boolean_true(tmp_path):
    """#22：非布尔 truthy 值（"yes"/1）不得被算作已人工审核（G9 前提）。"""
    db = tmp_path / "mini.db"
    _mini_db(db)
    rows = [{"query": "MySQL 索引", "category": "exact",
             "expect_q_keys": ["d1:q1"], "relevance": [{"q_key": "d1:q1", "grade": 3}],
             "reviewed": v} for v in ("yes", 1, True, False, None)]
    out = R.evaluate_golden(db, rows, k=10, mode="kw")
    assert out["reviewed_count"] == 1           # 只有布尔 True 那条
    assert [d["reviewed"] for d in out["details"]] == [False, False, True, False, False]


def _dup_db(path):
    """两行不同 doc_hash、同一归一题干（q_key 后缀相同）——跨文档同题。"""
    con = sqlite3.connect(str(path))
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
  qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER, tags_json TEXT,
  position TEXT, quality TEXT, q_key TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('d1','甲.pdf'),('d2','乙.pdf');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key)
VALUES (1,'d1','MySQL 索引为什么用 B+ 树','叶子节点链表。','八股','MySQL',3,'d1:q1'),
       (2,'d2','MySQL 索引为什么用 B+ 树','另一份答案。','八股','MySQL',3,'d2:q1');
INSERT INTO questions_fts(rowid, q_text, a_text)
VALUES (1,'MySQL 索引为什么用 B+ 树','叶子节点链表。'),
       (2,'MySQL 索引为什么用 B+ 树','另一份答案。');
""")
    con.commit()
    con.close()


def test_duplicate_identity_ndcg_bounded(tmp_path):
    """#20：重复归一身份只记一次分；每条 nDCG 必须在 [0,1]。"""
    db = tmp_path / "dup.db"
    _dup_db(db)
    rows = [{"query": "MySQL 索引", "category": "exact",
             "expect_q_keys": ["d1:q1"], "relevance": [{"q_key": "d1:q1", "grade": 3}],
             "reviewed": True}]
    out = R.evaluate_golden(db, rows, k=10, mode="kw")
    d = out["details"][0]
    assert 0.0 <= d["ndcg_at_10"] <= 1.0
    assert d["recall_at_5"] == 1.0
    assert out["ndcg_at_10"] <= 1.0


def test_grade_zero_gets_no_recall_credit(tmp_path):
    """#21：grade 0 的键不给 recall 记分；expect 与分级矛盾整条拒绝。"""
    db = tmp_path / "mini.db"
    _mini_db(db)
    rows = [{"query": "MySQL 索引", "category": "exact",
             "expect_q_keys": [], "relevance": [{"q_key": "d1:q1", "grade": 0}],
             "reviewed": True}]
    out = R.evaluate_golden(db, rows, k=10, mode="kw")
    d = out["details"][0]
    assert d["recall_at_5"] is None      # 无正相关项 → 不判 recall
    assert d["ndcg_at_10"] is None
    # expect 里的键被分级为 0 → 矛盾，拒绝
    bad = [{"query": "MySQL 索引", "expect_q_keys": ["d1:q1"],
            "relevance": [{"q_key": "d1:q1", "grade": 0}]}]
    with pytest.raises(ValueError, match="矛盾"):
        R.evaluate_golden(db, bad, k=10, mode="kw")
    # 非法 grade / 重复键同样拒绝
    with pytest.raises(ValueError, match="grade"):
        R._relevance({"relevance": [{"q_key": "d1:q1", "grade": 9}]})
    with pytest.raises(ValueError, match="重复"):
        R._relevance({"relevance": [{"q_key": "d1:q1", "grade": 1},
                                    {"q_key": "d2:q1", "grade": 2}]})
