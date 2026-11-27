# -*- coding: utf-8 -*-
"""seed_test_db：--force 也必须"先校验源库、再原子替换目标"，源库异常时旧测试库不丢。"""
import importlib.util
import json
from pathlib import Path

from mianjing_kb.kb import db

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "seed_test_db", ROOT / "scripts" / "seed_test_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert_question(conn, qid, seq, text):
    conn.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, span_start, span_end, "
        "qtype, conf, q_norm, q_norm_hash, simhash, extracted_at) "
        "VALUES (?, 'h', ?, ?, 'a', 0, 1, '八股', 'high', ?, '', 0, '2026-01-01T00:00:00')",
        (qid, seq, text, text))


def _src_db(path, questions):
    conn = db.connect(path)
    for seq, (qid, text) in enumerate(questions, start=1):
        _insert_question(conn, qid, seq, text)
    conn.commit()
    conn.close()


def _existing_test_db(path):
    conn = db.connect(path)
    _insert_question(conn, 1, 1, "旧题")
    conn.commit()
    conn.close()


def test_force_keeps_old_target_when_source_is_empty(tmp_path, capsys):
    mod = _load()
    src = tmp_path / "kb.sqlite"
    _src_db(src, [])
    dst = tmp_path / "kb_test.sqlite"
    _existing_test_db(dst)

    assert mod.main(["--src", str(src), "--dst", str(dst), "--force"]) == 2
    out = capsys.readouterr().out
    assert "questions 为空" in out
    # 旧测试库仍在，且原有的行没被清掉
    conn = db.connect(dst, create=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    finally:
        conn.close()


def test_force_refuses_target_that_is_the_source(tmp_path, capsys):
    mod = _load()
    src = tmp_path / "kb.sqlite"
    _src_db(src, [(1, "题")])
    assert mod.main(["--src", str(src), "--dst", str(src), "--force"]) == 2
    assert "同一文件" in capsys.readouterr().out
    assert src.exists()


def test_success_replaces_target_atomically(tmp_path, capsys):
    mod = _load()
    src = tmp_path / "kb.sqlite"
    _src_db(src, [(i, f"题{i}") for i in range(1, 4)])
    dst = tmp_path / "kb_test.sqlite"
    _existing_test_db(dst)

    assert mod.main(["--src", str(src), "--dst", str(dst), "--force", "--questions", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sampled"] == 2
    assert not list(tmp_path.glob("kb_test.sqlite.tmp-*"))
    conn = db.connect(dst, create=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM questions WHERE q_text='旧题'").fetchone()[0] == 0
    finally:
        conn.close()
