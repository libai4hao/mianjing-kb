"""只读 CLI 路径不得切换 SQLite journal mode 或修改数据库。"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from mianjing_kb.kb import db


def _delete_mode_db(path):
    conn = db.connect(path)
    conn.close()
    raw = sqlite3.connect(path)
    mode = raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    raw.close()
    assert mode.lower() == "delete"


def _journal_mode(path):
    raw = sqlite3.connect(path)
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        raw.close()


def _assert_still_delete(path):
    assert _journal_mode(path) == "delete"


@pytest.mark.parametrize("mode", ["stats", "plan", "dry_run"])
def test_extract_read_only_modes_do_not_switch_journal_mode(tmp_path, monkeypatch, mode):
    from mianjing_kb import extract

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    if mode in ("plan", "dry_run"):
        mapping = SimpleNamespace(by_doc={}, orphans={}, report={})
        monkeypatch.setattr(extract, "doc_pool", lambda _conn: ([], mapping))

    args = SimpleNamespace(
        db=str(path), stats=(mode == "stats"), plan=(mode == "plan"),
        dry_run=(mode == "dry_run"), require_gate=False,
        incremental=False, limit=0,
    )
    assert extract._run(args) == 0
    _assert_still_delete(path)


def test_classify_stats_uses_read_only_connection(tmp_path):
    from mianjing_kb import classify

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=False, stats=True, neighbor=False, show=0,
    )
    assert classify._run(args) == 0
    _assert_still_delete(path)


def test_classify_dry_run_uses_read_only_connection(tmp_path):
    from mianjing_kb import classify

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=True, stats=False, neighbor=False, show=0,
        status="pending_classify", redo=False, limit=0,
    )
    assert classify._run(args) == 0
    _assert_still_delete(path)


def test_bookmarks_dry_run_uses_read_only_connection(tmp_path):
    from mianjing_kb import bookmarks

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=True, only_merged=False, force=False,
    )
    assert bookmarks._run(args) == 0
    _assert_still_delete(path)
