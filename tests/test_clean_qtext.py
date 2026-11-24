# -*- coding: utf-8 -*-
"""clean_qtext：存量题库显示清洗，身份字段（q_norm/q_key）必须原样。"""
import json
import sqlite3
from pathlib import Path

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


def test_main_preview_is_readonly_and_apply_locks_target_parent(tmp_path, monkeypatch, capsys):
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(config, "BUILD_DIR", configured)
    target = tmp_path / "custom" / "kb.sqlite"
    target.parent.mkdir()
    con = sqlite3.connect(target)
    con.execute("CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_text TEXT)")
    con.execute("INSERT INTO questions VALUES(1,'**1** 、什么是 **Redis** ?')")
    con.commit()
    con.close()
    real_connect = cq.kbdb.connect
    real_lock = cq.wlock.write_lock
    real_backup = cq.kbdb.backup_db
    seen = []
    backup_seen = []

    def checked_connect(path, **kwargs):
        seen.append(kwargs.get("readonly"))
        return real_connect(path, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("预览不应拿写锁或做备份")

    def checked_backup(db_path, *args, **kwargs):
        locked, holder = cq.wlock.inspect_lock(target.parent / "locks" / "kb.lock")
        backup_seen.append((locked, (holder or {}).get("tool")))
        return real_backup(db_path, *args, **kwargs)

    monkeypatch.setattr(cq.kbdb, "connect", checked_connect)
    monkeypatch.setattr(cq.wlock, "write_lock", forbidden)
    monkeypatch.setattr(cq.kbdb, "backup_db", forbidden)
    assert cq.main(["--db", str(target)]) == 0
    capsys.readouterr()
    assert seen == [True] and backup_seen == []
    assert not (configured / "locks").exists()

    seen.clear()
    monkeypatch.setattr(cq.wlock, "write_lock", real_lock)
    monkeypatch.setattr(cq.kbdb, "backup_db", checked_backup)
    assert cq.main(["--db", str(target), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert seen == [False]
    assert backup_seen == [(True, "clean_qtext")]
    assert Path(out["backup"]).is_file()
    assert (target.parent / "locks" / "kb.lock").exists()
    assert not (configured / "locks").exists()
    con = sqlite3.connect(target)
    assert con.execute("SELECT q_text FROM questions").fetchone()[0] == "什么是 Redis?"
    con.close()
