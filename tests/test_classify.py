# -*- coding: utf-8 -*-
"""classify 的规则判定：topic / category / collection / source_trust / 未分类兜底。"""
import json
import sqlite3
from pathlib import Path

import pytest

from classify import main as classify_main
from intake import main as intake_main


@pytest.fixture()
def seeded(tmp_path):
    root = tmp_path / "corpus"
    layout = {
        "02-JVM/JVM调优实战.pdf": b"%PDF jvm",
        "11-面经与真题/11.2-大厂真题/字节跳动真题.pdf": b"%PDF byte",
        "11-面经与真题/11.3-专题总结包/面向字节系列/第一讲.pdf": b"%PDF series",
        "12-综合题库/Markdown版/原创PDF面试手册/计算机基础/算法刷题/Leetcode-Go.pdf": b"%PDF algo",
        "12-综合题库/Markdown版/MySQL/索引优化.md": b"%PDF mysql",
        "30-补录/12-综合题库/java面试综合整理版（大全）.docx": b"PK docx",
        "31-收件箱/陌生资料.pdf": b"%PDF unknown",
        "13-随便建的目录/随手放.pdf": b"%PDF random",
    }
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    db_path = tmp_path / "kb.sqlite"
    intake_main(["--root", str(root), "--db", str(db_path)])
    classify_main(["--db", str(db_path)])
    return db_path


def by_name(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    data = conn.execute("SELECT * FROM source_documents").fetchall()
    conn.close()
    return {r["file_name"]: dict(r) for r in data}


def test_filename_topic(seeded):
    row = by_name(seeded)["JVM调优实战.pdf"]
    assert row["topic"] == "JVM"
    assert row["topic_source"] == "l1_title"
    assert row["category"] == "题库"


def test_big_company_bank_structure(seeded):
    row = by_name(seeded)["字节跳动真题.pdf"]
    assert row["category"] == "真题"
    assert row["source_trust"] == "high"


def test_collection_from_deep_dir(seeded):
    row = by_name(seeded)["第一讲.pdf"]
    assert row["collection"] == "面向字节系列"
    assert row["category"] == "专题笔记"


def test_wrapper_segments_ignored(seeded):
    row = by_name(seeded)["Leetcode-Go.pdf"]
    # Markdown版 / 原创PDF面试手册 是包装词，算法刷题 是主题词，都不该当地名
    assert row["collection"] == "计算机基础"
    assert row["topic"] == "算法"


def test_topic_named_dir_is_not_a_collection(seeded):
    row = by_name(seeded)["索引优化.md"]
    # 12-综合题库/Markdown版/MySQL/ → 不能把 MySQL 当成来源集，退回上一个真实目录
    assert row["collection"] == "12-综合题库"
    assert row["topic"] == "MySQL"


def test_staging_prefix_stripped(seeded):
    row = by_name(seeded)["java面试综合整理版（大全）.docx"]
    assert row["category"] == "题库"                # 30-补录 前缀被剥掉后按 12-综合题库 判
    assert row["collection"] == "12-综合题库"


def test_unknown_dir_is_unclassified(seeded):
    for name in ("陌生资料.pdf", "随手放.pdf"):
        row = by_name(seeded)[name]
        assert row["category"] == "未分类"
        assert row["topic"] == "未分类"
        assert row["topic_source"] == "none"
        assert row["status"] == "classified"


def test_all_classified(seeded):
    conn = sqlite3.connect(str(seeded))
    left = conn.execute("SELECT COUNT(*) FROM source_documents WHERE status='pending_classify'").fetchone()[0]
    conn.close()
    assert left == 0
