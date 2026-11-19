# -*- coding: utf-8 -*-
"""document_products 产物台账：迁移、写入、增量判据、映射同步。"""
from __future__ import annotations

import os
import time

from mianjing_kb.kb import db as kbdb
from mianjing_kb.kb import mdmap, products


def test_table_created_by_migrations(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM document_products").fetchone()[0] == 0
    assert "015_document_products" in db_applied(conn)
    conn.close()


def db_applied(conn):
    return {r[0] for r in conn.execute("SELECT version FROM schema_version")}


def test_upsert_and_summary(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    md = tmp_path / "a.md"
    md.write_text("1. 什么是 JVM？\n\n答案。", encoding="utf-8")
    products.upsert(conn, "h1", md_rel="题库/a.md", md_hash="abcd1234", text_source="md",
                    input_path=md, questions_n=2, extracted_at="2026-11-19T10:00:00")
    products.upsert(conn, "h1", md_rel="题库/a.md", md_hash="abcd1234", text_source="md",
                    input_path=md, questions_n=3)
    row = products.get(conn, "h1")
    assert row["questions_n"] == 3 and row["extracted_at"] == "2026-11-19T10:00:00"
    assert row["input_size"] == md.stat().st_size
    s = products.summary(conn)["md"]
    assert s["rows"] == 1 and s["extracted"] == 1 and s["questions"] == 3
    conn.close()


def test_needs_work_detects_new_and_changed(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    md = tmp_path / "a.md"
    md.write_text("原文", encoding="utf-8")
    assert products.needs_work(conn, "h1", md) is True          # 无产物行
    products.upsert(conn, "h1", md_rel="a.md", md_hash="x", text_source="md",
                    input_path=md)                              # 没有 extracted_at
    assert products.needs_work(conn, "h1", md) is True
    products.upsert(conn, "h1", md_rel="a.md", md_hash="x", text_source="md",
                    input_path=md, extracted_at="now")
    assert products.needs_work(conn, "h1", md) is False
    time.sleep(0.01)
    md.write_text("原文改了", encoding="utf-8")
    os.utime(md, (time.time() + 5, time.time() + 5))
    assert products.needs_work(conn, "h1", md) is True
    conn.close()


def test_sync_mapping_records_md_and_orphan(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    md_root = tmp_path / "md"
    (md_root / "题库").mkdir(parents=True)
    md = md_root / "题库" / "甲__abcd1234.md"
    md.write_text("1. 题？\n\n答。", encoding="utf-8")
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("abcd1234ef00", "x", "甲.pdf", ".pdf", 1, "x/甲.pdf", "", "classified", "t", "now", "now"))
    m = mdmap.build(conn, md_root)
    n = products.sync_mapping(conn, m)
    assert n == 1 and m.by_doc
    assert products.get(conn, "abcd1234ef00")["text_source"] == "md"

    orphan_md = md_root / "题库" / "孤儿.md"
    orphan_md.write_text("1. 孤儿题？\n\n答。", encoding="utf-8")
    m2 = mdmap.build(conn, md_root)
    n2 = products.sync_mapping(conn, m2)
    assert n2 >= 2
    assert products.get(conn, mdmap.mirror_hash("题库/孤儿.md"))["text_source"] == "orphan"
    conn.close()
