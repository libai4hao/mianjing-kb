# -*- coding: utf-8 -*-
"""clean_qtext：存量题库显示清洗，身份字段（q_norm/q_key）必须原样。"""
import sqlite3

import pytest
from mianjing_kb import clean_qtext as cq
from mianjing_kb.kb import config


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "kb.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_text TEXT, q_norm TEXT, q_norm_hash TEXT,"
        " q_key TEXT);")
    con.executemany(
        "INSERT INTO questions(qid, q_text, q_norm, q_norm_hash, q_key) VALUES(?,?,?,?,?)",
        [(1, "**1** 、什么是 **Redis** ?", "1什么是redis", "h1", "d:h1"),
         (2, "以下代码 `int** p` 的输出？", "以下代码intp的输出", "h2", "d:h2"),
         (3, "什么是 JVM?", "什么是jvm", "h3", "d:h3"),
         (4, "1.5 版本有什么变化？", "15版本有什么变化", "h4", "d:h4")])
    con.commit()
    con.close()
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    return p


def test_dry_run_reports_without_writing(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = cq.clean_rows(con, apply=False)
    assert out["scanned"] == 4 and out["changed"] == 1 and out["applied"] is False
    assert con.execute("SELECT q_text FROM questions WHERE qid=1").fetchone()[0].startswith("**")
    con.close()


def test_apply_only_touches_q_text(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = cq.clean_rows(con, apply=True)
    assert out["changed"] == 1
    row = con.execute("SELECT q_text, q_norm, q_key FROM questions WHERE qid=1").fetchone()
    assert row["q_text"] == "什么是 Redis?"
    assert row["q_norm"] == "1什么是redis" and row["q_key"] == "d:h1"   # 身份不动
    # 代码/指针题干、小数版本、已干净题干都不动
    assert con.execute("SELECT q_text FROM questions WHERE qid=2").fetchone()[0] == \
        "以下代码 `int** p` 的输出？"
    assert con.execute("SELECT q_text FROM questions WHERE qid=4").fetchone()[0] == "1.5 版本有什么变化？"
    assert cq.clean_rows(con, apply=True)["changed"] == 0             # 幂等
    con.close()


def test_main_aborts_on_missing_db(tmp_path):
    assert cq.main(["--db", str(tmp_path / "missing.sqlite")]) == 2
    assert not (tmp_path / "missing.sqlite").exists()
