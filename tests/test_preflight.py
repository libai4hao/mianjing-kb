# -*- coding: utf-8 -*-
"""阶段 A：preflight 只读基线核对。"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mianjing_kb import preflight  # noqa: E402
from mianjing_kb.kb import db  # noqa: E402


def test_preflight_is_read_only_and_reports_schema(tmp_path, capsys):
    p = tmp_path / "kb.sqlite"
    conn = db.connect(p)
    conn.close()
    before = p.stat().st_mtime_ns

    assert preflight.main(["--db", str(p)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exists"] is True
    assert data["integrity_check"] == "ok"
    assert data["quick_check"] == "ok"
    assert data["pending_migrations"] == []
    assert "016_stable_question_keys" in data["applied_versions"]
    assert p.stat().st_mtime_ns == before


def test_preflight_missing_db_does_not_create(tmp_path, capsys):
    p = tmp_path / "missing.sqlite"
    assert preflight.main(["--db", str(p)]) == 2
    assert "数据库文件不存在" in capsys.readouterr().out
    assert not p.exists()


def test_preflight_empty_db_reports_all_migrations_pending(tmp_path, capsys):
    p = tmp_path / "empty.sqlite"
    sqlite3.connect(str(p)).close()
    assert preflight.main(["--db", str(p)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied_versions"] == []
    assert len(data["pending_migrations"]) == len(db.MIGRATIONS)
