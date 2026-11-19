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


def test_evaluate_golden_counts_hits_and_no_answer_false_positive(tmp_path):
    db = tmp_path / "mini.db"
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
