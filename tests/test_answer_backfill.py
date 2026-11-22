# -*- coding: utf-8 -*-
"""补答题（迁移 019）：目标筛选、写回留痕、拒答/失败分流。"""
import pytest
from mianjing_kb import answer_backfill as ab
from mianjing_kb.kb import db


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "kb.sqlite")
    rows = [
        (1, "什么是 JVM 类加载？", "", "boundary_gap"),
        (2, "Redis 为什么快？", "", "ok"),
        (3, "目录伪题", "", "toc_noise"),
        (4, "短", "", "boundary_gap"),
        (5, "已补过的题", "已有答案", "boundary_gap"),
    ]
    for qid, q, a, st in rows:
        c.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, answer_state, "
                  "q_norm, q_norm_hash, simhash, span_start, span_end, extracted_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, f"h{qid}", qid, q, a, st, f"n{qid}", f"h{qid}", 0, 0, 1, "now"))
    c.commit()
    yield c
    c.close()


def test_pick_targets_skips_toc_noise_answered_and_marked(conn):
    got = [q for q, _ in ab.pick_targets(conn)]
    assert got == [1, 4, 2]       # 3 目录伪题、5 已有答案被排除；4 太短留给 run 里筛


def test_run_writes_answer_with_provenance(conn, monkeypatch):
    def fake_chat(cfg, system, user):
        if "Redis" in user:
            return {"text": "NO_ANSWER"}
        return {"text": "类加载分五个阶段。"}
    monkeypatch.setattr(ab.llm_client, "chat", fake_chat)

    res = ab.run(conn, [(1, "什么是 JVM 类加载？"), (2, "Redis 为什么快？")], concurrency=1)
    assert res["answered"] == 1 and res["refused"] == 1
    row = conn.execute("SELECT a_text, answer_source, answer_model FROM questions WHERE qid=1").fetchone()
    assert row["a_text"] == "类加载分五个阶段。" and row["answer_source"] == "llm" and row["answer_model"]
    refused = conn.execute("SELECT a_text, answer_source FROM questions WHERE qid=2").fetchone()
    assert refused["a_text"] == "" and refused["answer_source"] == "llm_refused"


def test_run_dry_run_does_not_write(conn, monkeypatch):
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: {"text": "答案"})
    ab.run(conn, [(1, "什么是 JVM 类加载？")], concurrency=1, dry_run=True)
    row = conn.execute("SELECT a_text FROM questions WHERE qid=1").fetchone()
    assert row["a_text"] == ""


def test_stats_reports_remaining(conn):
    s = ab.stats(conn)
    assert s["todo"] == 3                 # 1/4/2 仍待补；3 目录伪题不计
    assert s["empty_by_source"]["(原文空)"] >= 4
