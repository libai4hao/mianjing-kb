# -*- coding: utf-8 -*-
"""SQLite 连接与迁移。表结构见 meta/schema/001_init.sql（docs/03 §10.2）。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA_FILE = Path(config.META_DIR) / "schema" / "001_init.sql"


def connect(db_path: Path | str | None = None, *, create: bool = True) -> sqlite3.Connection:
    """连接数据库；create=True 时自动建库并跑迁移。"""
    path = Path(db_path or config.DB_PATH)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if create:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """幂等建表（schema 全是 IF NOT EXISTS）。"""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


@contextmanager
def session(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_by_hash(conn: sqlite3.Connection, doc_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_documents WHERE doc_hash = ?", (doc_hash,)).fetchone()


def upsert_document(conn: sqlite3.Connection, row: dict) -> None:
    """按 doc_hash 插入或更新一条原文档记录。"""
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    sql = (
        f"INSERT INTO source_documents ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(doc_hash) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c != "doc_hash")
    )
    conn.execute(sql, [row[c] for c in cols])


def record_batch(conn: sqlite3.Connection, **kw) -> None:
    cols = list(kw.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO batches ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [kw[c] for c in cols],
    )


def count_by(conn: sqlite3.Connection, field: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT COALESCE({field},'<null>') AS k, COUNT(*) AS n FROM source_documents GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    return {r["k"]: r["n"] for r in rows}
