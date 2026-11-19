# -*- coding: utf-8 -*-
"""embed_questions：编码失败必须保住旧向量表，成功才事务替换。"""
import json
import os
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
    assert not list(tmp_path.glob("*vec-staging*"))   # 失败也要清掉本次标记/旁路


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


def _stale_marker(staging: Path, *, pid: int, process_start=None) -> Path:
    marker = E._staging_marker(staging)
    marker.write_text(json.dumps({"run_id": "old", "pid": pid,
                                  "process_start": process_start,
                                  "state": "ENCODING"}, ensure_ascii=False),
                      encoding="utf-8")
    return marker


def test_cleanup_removes_stale_staging_and_sidecars(tmp_path):
    """D-30：历史 staging + 侧车 + 标记必须一起清掉，不能只清主文件。"""
    db = tmp_path / "kb.db"
    staging = tmp_path / f".{db.name}.vec-staging-old.db"
    staging.write_bytes(b"stale")
    for suffix in ("-wal", "-shm"):
        Path(str(staging) + suffix).write_bytes(b"x")
    marker = _stale_marker(staging, pid=99999999)
    E._cleanup_stale_staging(db)
    assert not staging.exists()
    assert not Path(str(staging) + "-wal").exists()
    assert not Path(str(staging) + "-shm").exists()
    assert not marker.exists()


def test_cleanup_refuses_live_staging(tmp_path):
    """D-30：标记指向仍活着的任务 → 中止新任务，绝不误删。"""
    db = tmp_path / "kb.db"
    staging = tmp_path / f".{db.name}.vec-staging-live.db"
    staging.write_bytes(b"active")
    _stale_marker(staging, pid=os.getpid(),
                  process_start=E._process_start(os.getpid()))
    with pytest.raises(RuntimeError, match="仍在运行"):
        E._cleanup_stale_staging(db)
    assert staging.exists()


def test_cleanup_deletes_pid_reuse_marker(tmp_path):
    """D-30：PID 被复用（创建时间不一致）→ 按残留清理，不误判为活任务。"""
    pytest.importorskip("psutil")
    db = tmp_path / "kb.db"
    staging = tmp_path / f".{db.name}.vec-staging-reuse.db"
    staging.write_bytes(b"stale")
    start = E._process_start(os.getpid())
    _stale_marker(staging, pid=os.getpid(),
                  process_start=(start - 3600 if start else 1))
    E._cleanup_stale_staging(db)
    assert not staging.exists()


def test_cleanup_failure_aborts(tmp_path):
    """D-30：清理失败必须报错中止，不能 pass 吞掉。"""
    db = tmp_path / "kb.db"
    staging = tmp_path / f".{db.name}.vec-staging-stuck.db"
    staging.mkdir()                         # 目录无法 unlink → 天然触发 OSError
    _stale_marker(staging, pid=99999999)
    with pytest.raises(RuntimeError, match="清理失败"):
        E._cleanup_stale_staging(db)
    assert staging.exists()
