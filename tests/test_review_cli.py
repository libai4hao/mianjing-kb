# -*- coding: utf-8 -*-
"""P2-③ review_cli 测试：灰区汇总计数 + resolve 状态落库。"""
import sqlite3

import pytest

from mianjing_kb import review_cli
from mianjing_kb.kb import config


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    conn = sqlite3.connect(str(build / "kb.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.executescript((config.META_DIR / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    conn.executescript((config.META_DIR / "schema" / "004_questions.sql").read_text(encoding="utf-8"))
    # dispatch 计划：1 review + 1 suggest
    (build / "dispatch_plan.csv").write_text(
        "rel,state,reason,route,status,alt,ocr_txt\n"
        "a/Leetcode-Go.pdf,review,低CJK,review,classified,,\n"
        "b/旧版.pdf,fail,乱码,supersede_suggest,classified,,\n",
        encoding="utf-8")
    # 隔离件 1 + 待裁定件（Leetcode-Go，classified）+ 低信度题 1
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES "
        "('q1' || 'x' || '00000000000000', 'q1000000000000000', '隔离件.pdf', '.pdf', 1, "
        "'x/隔离件.pdf', '', 'quarantined', 't', 'now', 'now')")
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES "
        "('lg0000000000000001', 'lg0000000000000001', 'Leetcode-Go.pdf', '.pdf', 1, "
        "'a/Leetcode-Go.pdf', '', 'classified', 't', 'now', 'now')")
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
        "q_norm, q_norm_hash, simhash, conf, qtype, extracted_at) VALUES "
        "('q1000000000000000', 1, '低信度题？', '', 0, 1, '低信度题', 'h', 0, 'low', '八股', 'now')")
    conn.commit()
    yield corpus, conn
    conn.close()


def test_gray_counts(tmp_env):
    corpus, conn = tmp_env
    g = review_cli.gray(conn)
    assert g["review"]["count"] == 1
    assert g["suggest"]["count"] == 1
    assert g["quarantined"]["count"] == 1
    assert g["lowconf"]["count"] == 1


def test_resolve_escapes_like_wildcards(tmp_env):
    """文件名里的 '_' 不能当 LIKE 通配符：A_B.pdf 不能连带命中 AXB.pdf。"""
    corpus, conn = tmp_env
    for h, name in (("aaa0000000000001", "A_B.pdf"), ("bbb0000000000002", "AXB.pdf")):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (h, h + "0" * 48, name, ".pdf", 1, f"c/{name}", "c", "classified", "t", "now", "now"))
    conn.commit()
    out = review_cli.resolve(conn, "A_B", "superseded", "退役")
    assert out["ok"] is True and out["file_name"] == "A_B.pdf"


def test_resolve_appends_note_instead_of_overwriting(tmp_env):
    """converted_to / replaced_by / needs_convert 等留痕必须保留（G5 与人工都要看）。"""
    corpus, conn = tmp_env
    conn.execute("UPDATE source_documents SET note='needs_convert' "
                 "WHERE file_name='Leetcode-Go.pdf'")
    conn.commit()
    review_cli.resolve(conn, "Leetcode-Go", "superseded", "测试退役")
    note = conn.execute(
        "SELECT note FROM source_documents WHERE file_name='Leetcode-Go.pdf'").fetchone()["note"]
    assert note.startswith("needs_convert |")
    assert "[review_cli" in note and "测试退役" in note


def test_resolve_updates_status(tmp_env):
    corpus, conn = tmp_env
    out = review_cli.resolve(conn, "Leetcode-Go", "superseded", "测试退役")
    assert out["ok"] and out["new_status"] == "superseded"
    row = conn.execute(
        "SELECT status, note FROM source_documents WHERE file_name='Leetcode-Go.pdf'").fetchone()
    assert row["status"] == "superseded" and "[review_cli" in row["note"]
    # 非法状态被拒
    assert review_cli.resolve(conn, "Leetcode-Go", "quarantined", "x")["ok"] is False
    # 未匹配件报错
    assert review_cli.resolve(conn, "不存在.pdf", "superseded", "x")["ok"] is False
