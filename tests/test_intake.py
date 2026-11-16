# -*- coding: utf-8 -*-
"""intake 的判重行为：新增 / 重复 / 改名移动 / 删除后回归。"""
import json
import sqlite3
from pathlib import Path

import pytest

from intake import main


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "corpus"
    (root / "02-JVM").mkdir(parents=True)
    (root / "02-JVM" / "JVM面试题.pdf").write_bytes(b"%PDF-1.4 fake jvm content")
    (root / "12-综合题库").mkdir(parents=True)
    (root / "12-综合题库" / "笔记.md").write_text("内容", encoding="utf-8")
    (root / "99-重复文件待删").mkdir(parents=True)
    (root / "99-重复文件待删" / "旧件.pdf").write_bytes(b"%PDF-1.4 old dup")
    return root


def rows(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out = conn.execute("SELECT * FROM source_documents").fetchall()
    conn.close()
    return out


def test_first_scan_adds_and_quarantines(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(corpus), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["added"] == 2
    assert summary["actions"]["quarantined"] == 1
    data = rows(db_path)
    assert len(data) == 3
    statuses = {r["file_name"]: r["status"] for r in data}
    assert statuses["旧件.pdf"] == "quarantined"
    assert statuses["JVM面试题.pdf"] == "pending_classify"


def test_rescan_is_idempotent(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["duplicate"] == 3
    assert summary["actions"].get("added", 0) == 0
    assert len(rows(db_path)) == 3


def test_rename_is_moved_not_new(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "02-JVM" / "JVM面试题.pdf").rename(corpus / "02-JVM" / "JVM面试题-改名.pdf")
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["renamed"] == 1
    assert len(rows(db_path)) == 3
    renamed = [r for r in rows(db_path) if r["file_name"].endswith("改名.pdf")]
    # 改名只更新路径与文件名，不能把状态降级（否则已入库成果会被"回滚"）
    assert renamed and renamed[0]["status"] == "pending_classify"


def test_deleted_then_returned_is_resurrected(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    target = corpus / "12-综合题库" / "笔记.md"
    raw = target.read_bytes()
    target.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE source_documents SET status='absent' WHERE file_name='笔记.md'")
    conn.commit()
    conn.close()

    target.write_bytes(raw)
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"]["resurrected"] == 1


def test_dry_run_with_existing_db_does_not_write(corpus, tmp_path, capsys):
    """库已存在时 --dry-run 用只读连接，但仍要能口径一致地报重复数，且不写库（曾 crash）。"""
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "02-JVM" / "新增件.pdf").write_bytes(b"%PDF-1.4 brand new")
    assert main(["--root", str(corpus), "--db", str(db_path), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"].get("candidate", 0) == 0   # 库已存在，不是候选统计模式
    assert summary["actions"]["added"] == 1
    assert summary["actions"]["duplicate"] == 3
    assert len(rows(db_path)) == 3                     # 只读，没新增行


def test_copy_in_staging_is_duplicate_not_moved(corpus, tmp_path, capsys):
    """往收件箱丢一份库内已有文件的副本：应判 duplicate，绝不能把库内记录"搬"到收件箱。"""
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "31-收件箱").mkdir()
    (corpus / "31-收件箱" / "JVM面试题.pdf").write_bytes((corpus / "02-JVM" / "JVM面试题.pdf").read_bytes())
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["duplicate"] >= 1
    assert summary["actions"].get("moved", 0) == 0
    row = [r for r in rows(db_path) if r["file_name"] == "JVM面试题.pdf"][0]
    assert row["dir_path"] == "02-JVM"          # 原件位置没被副本夺走


def test_dry_run_writes_nothing(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(corpus), "--db", str(db_path), "--dry-run"]) == 0
    capsys.readouterr()
    assert not db_path.exists()


def test_quarantine_copy_cannot_steal_path(tmp_path, capsys):
    """隔离区里的同内容件只算 duplicate，不允许把库内记录的路径改到 99/ 下。

    ADR D-12：曾出现 12-综合题库 的正式件被 99-重复文件待删 的同内容副本"夺主"，
    记录路径被改写、且混进了可入库清单。
    """
    root = tmp_path / "corpus"
    real = root / "12-综合题库" / "设计模式面试专题.md"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"same content")
    dup = root / "99-重复文件待删" / "12-综合题库" / "设计模式面试专题.md"
    dup.parent.mkdir(parents=True)
    dup.write_bytes(b"same content")

    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()

    from kb import hashing
    row = [r for r in rows(db_path) if r["doc_hash"] == hashing.sha256_file(real)[:16]]
    assert len(row) == 1
    assert row[0]["dir_path"] == "12-综合题库"
    assert row[0]["status"] == "pending_classify"
    assert row[0]["parent_dir"] == "12-综合题库"


def test_move_into_quarantine_flips_status(tmp_path, capsys):
    """过时件清理：正式区记录被移进 99- 区后，路径要改、状态要转 quarantined、不再进清单。"""
    root = tmp_path / "corpus"
    src = root / "05-Redis与缓存" / "memcached面试专题.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF old topic")
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()

    dest_dir = root / "99-重复文件待删" / "05-Redis与缓存"
    dest_dir.mkdir(parents=True)
    src.rename(dest_dir / "memcached面试专题.pdf")

    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"]["to_quarantine"] == 1
    assert summary["actions"].get("moved", 0) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT dir_path, status FROM source_documents WHERE file_name='memcached面试专题.pdf'").fetchone())
    in_queue = conn.execute("SELECT COUNT(*) FROM ingest_queue WHERE file_name='memcached面试专题.pdf'").fetchone()[0]
    conn.close()
    assert row["status"] == "quarantined"
    assert row["dir_path"] == "99-重复文件待删/05-Redis与缓存"
    assert in_queue == 0


def test_derived_tree_never_becomes_source(tmp_path, capsys):
    """99 区里镜像的 20-题库MD 派生件不能当新源登记（否则过时件清理会把派生 md 变成行）。"""
    root = tmp_path / "corpus"
    derived = root / "99-重复文件待删" / "20-题库MD" / "题库" / "Redis" / "memcached面试专题.md"
    derived.parent.mkdir(parents=True)
    derived.write_text("# 派生件", encoding="utf-8")
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()
    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
    conn.close()
    assert total == 0
