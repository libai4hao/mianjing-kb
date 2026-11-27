# -*- coding: utf-8 -*-
"""书签扫描（tools/bookmarks.py）：合并版源边界落库的回归用例。"""
import json
import sqlite3
from pathlib import Path

import pymupdf

from mianjing_kb import bookmarks


def _make_merged(path: Path, titles_pages: list[tuple[str, int]], total_pages: int):
    doc = pymupdf.open()
    for _ in range(total_pages):
        doc.new_page()
    doc.set_toc([[1, t, p] for t, p in titles_pages])
    doc.save(str(path))
    doc.close()


def test_spans_from_toc_computes_inclusive_ranges():
    toc = [[1, "a.pdf", 1], [1, "b.pdf", 7], [1, "c.pdf", 11], [2, "sub", 12]]
    count, spans = bookmarks.spans_from_toc(toc, 23, "merged.pdf")
    assert count == 3
    assert spans == [
        {"name": "a.pdf", "page_start": 1, "page_end": 6},
        {"name": "b.pdf", "page_start": 7, "page_end": 10},
        {"name": "c.pdf", "page_start": 11, "page_end": 23},
    ]


def test_spans_from_toc_without_bookmarks_falls_back_and_marks_it():
    count, spans = bookmarks.spans_from_toc([], 5, "plain.pdf")
    assert count == 1
    assert spans[0]["page_start"] == 1 and spans[0]["page_end"] == 5
    assert spans[0]["from"] == "fallback_no_source_toc"


def test_chapter_titles_are_not_sources():
    """普通 PDF 的 L1 是章节标题（不带 .pdf），必须按单源处理。
    干跑实测：若把 L1 条数一律当源数，291 份 PDF 会算出 119 份合并版 / 3,338 个源。"""
    toc = [[1, "第 1 章 Java 概述", 1], [1, "第 2 章 集合框架", 3], [1, "第 3 章 并发", 5]]
    count, spans = bookmarks.spans_from_toc(toc, 8, "Java基础核心总结.pdf")
    assert count == 1
    assert spans[0]["name"] == "Java基础核心总结.pdf"
    assert spans[0]["page_end"] == 8


def test_mixed_toc_counts_only_filename_l1():
    toc = [[1, "51道.pdf", 1], [2, "第 1 章", 2], [1, "JVM.pdf", 5]]
    count, spans = bookmarks.spans_from_toc(toc, 9, "merged.pdf")
    assert count == 2
    assert spans[0]["page_end"] == 4 and spans[1]["page_start"] == 5


def test_bookmarks_written_to_db(tmp_path, capsys, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # #81 后 src_path 须解析在语料根内：夹具同步 config（与生产口径一致）
    from kb import config as kconfig
    monkeypatch.setattr(kconfig, "CORPUS_ROOT", corpus)
    pdf = corpus / "【合并版】JVM面试题合集.pdf"
    _make_merged(pdf, [("51道.pdf", 1), ("大厂.pdf", 4)], 6)

    db_path = tmp_path / "kb.sqlite"
    # 用真实迁移建库（含 source_count/sources_json），再塞一条待扫记录
    from kb import db as kdb

    conn = kdb.connect(db_path)
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("h1", "0" * 64, pdf.name, ".pdf", pdf.stat().st_size, str(pdf), "", "classified", "t",
         "2026-11-17T00:00:00", "2026-11-17T00:00:00"),
    )
    conn.commit()
    conn.close()

    assert bookmarks.main(["--db", str(db_path)]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT source_count, sources_json FROM source_documents").fetchone()
    conn.close()
    assert row[0] == 2
    spans = json.loads(row[1])
    assert spans[0] == {"name": "51道.pdf", "page_start": 1, "page_end": 3}
    assert spans[1]["page_end"] == 6
