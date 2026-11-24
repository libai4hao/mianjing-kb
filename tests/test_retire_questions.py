# -*- coding: utf-8 -*-
"""retire_questions：按 doc_hash 退役题目，连带 FTS / 向量 / canonical 依赖。"""
import json
import sqlite3
from pathlib import Path

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


def test_apply_scopes_canonical_cleanup_candidates_and_witnesses(tmp_path):
    path = tmp_path / "kb.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_key TEXT);
        CREATE TABLE canonical_questions(canonical_id TEXT PRIMARY KEY, witness INTEGER, updated_at TEXT);
        CREATE TABLE canonical_dependents(canonical_id TEXT, q_key TEXT, doc_hash TEXT, collection TEXT);
        CREATE TABLE question_variants(canonical_id TEXT, q_key TEXT, doc_hash TEXT);
        CREATE TABLE merge_candidates(q_key TEXT PRIMARY KEY, canonical_id TEXT, status TEXT);
        INSERT INTO questions VALUES(1,'gone','gone:h1'),(2,'gone','gone:h2'),(3,'keep','keep:h3');
        INSERT INTO canonical_questions VALUES('affected',2,'old'),('survivor',2,'old'),('unrelated-orphan',9,'old');
        INSERT INTO canonical_dependents VALUES
          ('affected','gone:h1','gone','c1'),('survivor','gone:h2','gone','c1'),
          ('survivor','keep:h3','keep','c2');
        INSERT INTO question_variants VALUES
          ('affected','gone:h1','gone'),('survivor','gone:h2','gone'),('survivor','keep:h3','keep');
        INSERT INTO merge_candidates VALUES
          ('gone:h1','affected','pending'),('keep:h3','survivor','approved'),
          ('other:h9','unrelated-orphan','rejected');
        """)
    con.commit()
    con.close()

    con = sqlite3.connect(path)
    res = rq.retire(con, ["gone"], apply=True)
    con.commit()
    con.close()
    assert res["canonical_questions_removed"] == 1
    assert res["merge_candidates_removed"] == 1
    con = sqlite3.connect(path)
    assert con.execute("SELECT canonical_id FROM canonical_questions ORDER BY 1").fetchall() == [
        ("survivor",), ("unrelated-orphan",)]
    assert con.execute("SELECT witness FROM canonical_questions WHERE canonical_id='survivor'"
                       ).fetchone()[0] == 1
    assert con.execute("SELECT q_key,status FROM merge_candidates ORDER BY 1").fetchall() == [
        ("keep:h3", "approved"), ("other:h9", "rejected")]
    con.close()


def test_main_preview_skips_lock_and_apply_uses_target_parent(tmp_path, monkeypatch, capsys):
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(config, "BUILD_DIR", configured)
    target = tmp_path / "custom" / "online.db"
    target.parent.mkdir()
    con = sqlite3.connect(target)
    con.execute("CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_key TEXT)")
    con.execute("INSERT INTO questions VALUES(1,'gone','gone:h1')")
    con.commit()
    con.close()
    calls = []
    backup_seen = []
    real_lock = rq.wlock.write_lock
    real_backup = rq.kbdb.backup_db

    def checked(*args, **kwargs):
        calls.append((args, kwargs))
        return real_lock(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("预览不应拿写锁或做备份")

    def checked_backup(db_path, *args, **kwargs):
        locked, holder = rq.wlock.inspect_lock(target.parent / "locks" / "kb.lock")
        backup_seen.append((locked, (holder or {}).get("tool")))
        return real_backup(db_path, *args, **kwargs)

    monkeypatch.setattr(rq.wlock, "write_lock", checked)
    monkeypatch.setattr(rq.kbdb, "backup_db", forbidden)
    assert rq.main(["--db", str(target), "--doc-hash", "gone"]) == 0
    capsys.readouterr()
    assert calls == [] and backup_seen == []
    monkeypatch.setattr(rq.kbdb, "backup_db", checked_backup)
    assert rq.main(["--db", str(target), "--doc-hash", "gone", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert calls[0][1]["build_dir"] == target.parent
    assert calls[0][1]["target"] == target
    assert backup_seen == [(True, "retire_questions")]
    assert Path(out["backup"]).is_file()
    assert not (configured / "locks").exists()
    assert (target.parent / "locks" / "kb.lock").exists()
