# -*- coding: utf-8 -*-
"""retire_questions：按 doc_hash 退役题目，连带 FTS / 向量 / canonical 依赖。"""
import sqlite3

import pytest
from mianjing_kb import retire_questions as rq
from mianjing_kb.kb import config


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "kb.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        """
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, q_norm_hash TEXT, q_key TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
CREATE TABLE canonical_questions(canonical_id TEXT PRIMARY KEY, title_norm TEXT);
CREATE TABLE canonical_dependents(canonical_id TEXT, q_key TEXT, doc_hash TEXT, collection TEXT);
CREATE TABLE question_variants(variant_id INTEGER, canonical_id TEXT, doc_hash TEXT, q_key TEXT);
CREATE TABLE label_facts(q_norm_hash TEXT, topic TEXT);
""")
    con.executemany("INSERT INTO questions VALUES(?,?,?,?,?)", [
        (1, "gone", "旧题一", "h1", "gone:h1"),
        (2, "gone", "旧题二", "h2", "gone:h2"),
        (3, "keep", "留存题", "h3", "keep:h3")])
    con.executemany("INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(?,?,?)", [
        (1, "旧题一", "a"), (2, "旧题二", "a"), (3, "留存题", "a")])
    con.executemany("INSERT INTO canonical_questions(canonical_id, title_norm) VALUES(?,?)", [
        ("c1", "c"), ("c2", "c")])
    con.executemany("INSERT INTO canonical_dependents VALUES(?,?,?,?)", [
        ("c1", "gone:h1", "gone", ""), ("c1", "gone:h2", "gone", ""),
        ("c2", "keep:h3", "keep", "")])
    con.executemany("INSERT INTO question_variants VALUES(?,?,?,?)", [
        (1, "c1", "gone", "gone:h1"), (2, "c2", "keep", "keep:h3")])
    con.execute("INSERT INTO label_facts VALUES('h1','并发')")
    con.commit(); con.close()
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    return p


def test_dry_run_reports_without_deleting(db):
    con = sqlite3.connect(db)
    res = rq.retire(con, ["gone"], apply=False)
    assert res["questions"] == 2 and res["applied"] is False
    assert res["tables"]["questions_fts"] == 2
    assert res["tables"]["canonical_dependents"] == 2
    assert res["label_facts_touched"] == 1
    assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 3
    con.close()


def test_apply_removes_rows_and_prunes_empty_canonical(db):
    con = sqlite3.connect(db)
    res = rq.retire(con, ["gone"], apply=True)
    assert res["applied"] is True and res["canonical_questions_removed"] == 1
    assert con.execute("SELECT qid FROM questions ORDER BY qid").fetchall() == [(3,)]
    assert con.execute("SELECT rowid FROM questions_fts ORDER BY rowid").fetchall() == [(3,)]
    assert con.execute("SELECT canonical_id FROM canonical_questions").fetchall() == [("c2",)]
    assert con.execute("SELECT canonical_id FROM canonical_dependents").fetchall() == [("c2",)]
    assert con.execute("SELECT canonical_id FROM question_variants").fetchall() == [("c2",)]
    assert con.execute("SELECT COUNT(*) FROM label_facts").fetchone()[0] == 1   # 词级事实不动
    con.close()


def test_main_rejects_missing_db(tmp_path):
    assert rq.main(["--db", str(tmp_path / "missing.sqlite"), "--doc-hash", "x"]) == 2
    assert not (tmp_path / "missing.sqlite").exists()
