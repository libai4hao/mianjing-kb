# -*- coding: utf-8 -*-
"""batch_label 金标扩样（T20）：从 questions 分层抽样，跳过已进 golden_labels 的题干。"""
import json

from mianjing_kb import batch_label as bl
from mianjing_kb.kb import db as kbdb

FIXED = {"conf": "low", "extracted_at": "2026-11-26T00:00:00", "q_norm": "", "simhash": 0}


def _seed(conn):
    rows = [
        ("h1", "Java基础", "八股", 3, "[]", "什么是 JVM 的类加载？", "分五步。"),
        ("h2", "Java基础", "八股", 2, "[]", "synchronized 和 ReentrantLock 区别？", "答：…"),
        ("h3", "数据库", "八股", 4, "[]", "MySQL 索引失效的场景有哪些？", "答：…"),
        ("h4", "HR与求职", "HR", 1, "[]", "为什么离职？", "答：…"),
        ("h5", None, "八股", None, None, "没打过 topic 的题？", "答：…"),
    ]
    for i, (h, topic, qtype, diff, tags, q, a) in enumerate(rows):
        conn.execute(
            "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
            "qtype, conf, q_norm, q_norm_hash, simhash, extracted_at, topic, difficulty, tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("d1", i, q, a, 0, len(q), qtype, FIXED["conf"], q, h, FIXED["simhash"],
             FIXED["extracted_at"], topic, diff, tags))
    conn.execute("INSERT INTO golden_labels (q_norm_hash, q_text, topic_gold, annotator, created_at) "
                 "VALUES ('h3', 'MySQL 索引失效的场景有哪些？', '数据库', 'human', '2026-11-26')")
    conn.commit()


def test_golden_export_stratifies_and_skips_existing(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    _seed(conn)
    out = tmp_path / "golden_todo.json"
    r = bl.golden_export(conn, 3, out=out)
    assert r["sampled"] == 3 and r["strata"] == 2      # 数据库那条已在金标里，整层被跳过
    items = json.loads(out.read_text(encoding="utf-8"))["items"]
    assert all(it["topic_gold"] == "" for it in items)
    assert "h3" not in {it["q_norm_hash"] for it in items}      # 已在金标里的跳过
    assert {it["current"]["topic"] for it in items} == {"Java基础", "HR与求职"}
    assert "h5" not in {it["q_norm_hash"] for it in items}      # 没 topic 的行不进金标
    conn.close()


def test_golden_export_refuses_to_overwrite_pending(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    _seed(conn)
    out = tmp_path / "golden_todo.json"
    assert bl.golden_export(conn, 2, out=out)["sampled"] == 2
    again = bl.golden_export(conn, 2, out=out)
    assert again["ok"] is False and "已存在待回灌" in again["note"]
    conn.close()
