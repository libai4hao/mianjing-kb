# -*- coding: utf-8 -*-
"""embed_questions：编码失败必须保住旧向量表，成功才事务替换。"""
import sqlite3
from pathlib import Path

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")
from mianjing_kb import embed_questions as E  # noqa: E402


def _mk_db(path: Path, rows=((1, "什么是 JVM"), (2, "MySQL 索引"))):
    con = sqlite3.connect(str(path))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_text TEXT, topic TEXT, "
        "qtype TEXT, difficulty INTEGER, q_key TEXT, doc_hash TEXT, q_norm_hash TEXT)")
    con.executemany(
        "INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)",
        [(qid, text, "JVM", "八股", 2, f"k{qid}", "d", f"n{qid}") for qid, text in rows])
    E.create_vec_table(con)
    con.execute(
        "INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty, q_key) "
        "VALUES (?,?,?,?,?,?)",
        (1, sqlite_vec.serialize_float32([9.0] * E.DIM), "old", "old", 1, "old"))
    E.write_vec_meta(con, "old-hash")
    con.commit()
    con.close()


def _count(path: Path) -> int:
    con = sqlite3.connect(str(path))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    try:
        return con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
    finally:
        con.close()


class FakeModel:
    def __init__(self, fail=False):
        self.fail = fail

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("onnx boom")
        return [[0.1] * E.DIM for _ in texts]


def test_model_load_failure_keeps_old_vectors(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    _mk_db(db)
    monkeypatch.setattr(E, "get_model", lambda: (_ for _ in ()).throw(RuntimeError("model boom")))
    with pytest.raises(RuntimeError, match="model boom"):
        E.run(db)
    assert _count(db) == 1


def test_encode_failure_keeps_old_vectors(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    _mk_db(db)
    monkeypatch.setattr(E, "get_model", lambda: FakeModel(fail=True))
    with pytest.raises(RuntimeError, match="onnx boom"):
        E.run(db)
    assert _count(db) == 1


def test_success_replaces_vectors_and_writes_meta(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    _mk_db(db)
    monkeypatch.setattr(E, "get_model", lambda: FakeModel())
    out = E.run(db, batch=1)
    assert out["vectors"] == 2 and out["questions"] == 2
    con = sqlite3.connect(str(db))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    try:
        assert con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0] == 2
        assert E.read_vec_meta(con)["question_set_hash"] == out["question_set_hash"]
        assert not con.execute(
            "SELECT COUNT(*) FROM vec_questions WHERE topic='old'").fetchone()[0]
    finally:
        con.close()
    assert not list(tmp_path.glob(f".{db.name}.vec-staging-*"))
