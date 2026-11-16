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


def test_dry_run_writes_nothing(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(corpus), "--db", str(db_path), "--dry-run"]) == 0
    capsys.readouterr()
    assert not db_path.exists()
