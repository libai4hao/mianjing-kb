"""状态常量（T13）：常量与 schema CHECK 枚举不许漂移，分组语义有守护。"""
import re
import sqlite3

import pytest

from mianjing_kb.kb import db, status


def _cols_sql(tmp_path):
    conn = db.connect(tmp_path / "kb.sqlite")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='source_documents'").fetchone()[0]
    conn.close()
    return sql


def _check_enum(sql: str) -> set[str]:
    m = re.search(r"CHECK\s*\(\s*status IN\s*\(([^)]*)\)", sql, re.S)
    assert m, "source_documents.status 的 CHECK 约束没找到"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_all_matches_schema_check_enum(tmp_path):
    """status.ALL 必须与 001_init.sql 的 CHECK 枚举逐字一致（新增状态两边一起改）。"""
    assert _check_enum(_cols_sql(tmp_path)) == set(status.ALL)


def test_every_status_is_insertable_and_unknown_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "kb.sqlite")
    for i, st in enumerate(status.ALL):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, src_path, dir_path, "
            "status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"h{i}", "x" * 64, "a.pdf", "/a.pdf", "", st, "b", "t", "t"))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, src_path, dir_path, "
            "status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("bogus", "x" * 64, "a.pdf", "/a.pdf", "", "classifed", "b", "t", "t"))
    conn.close()


def test_sql_in_renders_and_validates():
    assert status.sql_in((status.CLASSIFIED, status.SUPERSEDED)) == "('classified','superseded')"
    with pytest.raises(ValueError):
        status.sql_in(("classifed",))


def test_groups_semantics():
    """题池只认 classified；已退役/隔离不进后继记录有效态。"""
    assert status.EXTRACTABLE == (status.CLASSIFIED,)
    assert set(status.RETIRED) == {status.SUPERSEDED, status.ABSENT}
    assert status.QUARANTINED not in status.SUCCESSOR_OK
    assert status.SUPERSEDED not in status.SUCCESSOR_OK
    assert status.CLASSIFIED in status.SUCCESSOR_OK
    # 预留态必须都真实存在于枚举里（doctor 检查用）
    assert all(v in status.ALL for v in status.RESERVED)
