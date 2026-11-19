# -*- coding: utf-8 -*-
"""标签质量门禁回归：碎片 / pending 未复核 / 其他主题率。"""
import sqlite3

from mianjing_kb import batch_label, label_quality


def _conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE questions (qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT,
                                a_text TEXT, q_norm_hash TEXT, topic TEXT);
        CREATE TABLE source_documents (doc_hash TEXT, topic TEXT);
        CREATE TABLE labels_staging (qid INTEGER PRIMARY KEY, topic TEXT, qtype TEXT,
                                     difficulty INTEGER, review_state TEXT);
    """)
    for qid, text, topic, state in rows:
        conn.execute("INSERT INTO questions(qid, q_text, topic) VALUES(?,?,NULL)",
                     (qid, text))
        conn.execute("INSERT INTO labels_staging(qid, topic, qtype, difficulty, review_state) "
                     "VALUES(?,?, '八股', 2, ?)", (qid, topic, state))
    return conn


def test_quality_pass():
    rows = [(i, f"什么是第{i}个知识点?", "后端", "agree") for i in range(1, 21)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "PASS" and q["ok"] is True


def test_quality_blocks_code_fragment():
    rows = [(1, "#include <stdio.h>", "C++", "agree")] + [
        (i, f"什么是第{i}个知识点?", "后端", "agree") for i in range(2, 11)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "FAIL"
    assert any("code_fragment" in f for f in q["fails"])
    assert q["detail"]["fragment_qids"] == [1]


def test_quality_blocks_pending():
    rows = [(i, f"什么是第{i}个知识点?", "后端", "pending" if i == 3 else "agree")
            for i in range(1, 11)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "FAIL"
    assert any("pending_review" in f for f in q["fails"])


def test_quality_other_topic_hard_and_warn():
    hard = [(i, f"什么是第{i}个知识点?", "其他" if i <= 6 else "后端", "agree")
            for i in range(1, 21)]
    assert label_quality.assess(_conn(hard))["state"] == "FAIL"  # 30%
    warn = [(i, f"什么是第{i}个知识点?", "其他" if i <= 4 else "后端", "agree")
            for i in range(1, 21)]
    q = label_quality.assess(_conn(warn))                        # 20%
    assert q["state"] == "WARN" and q["ok"] is True


def test_pick_targets_skips_code_fragments():
    conn = _conn([(1, "#include <stdio.h>", "C++", "agree"),
                  (2, "什么是虚函数?", "C++", "agree"),
                  (3, "int top = 0 ;", "C++", "agree")])
    conn.execute("UPDATE questions SET topic='未分类'")
    stats: dict = {}
    got = [r["qid"] for r in batch_label.pick_targets(conn, skip_code=True, stats=stats)]
    assert got == [2]
    assert stats["skipped_code"] == 2
    got2 = [r["qid"] for r in batch_label.pick_targets(conn, skip_code=False)]
    assert got2 == [1, 2, 3]
