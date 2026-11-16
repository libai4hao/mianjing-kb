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
    """幂等迁移：schema 用 IF NOT EXISTS；需要 ADD COLUMN 的用 pragma 先探。"""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)
    _ensure_parent_dir(conn)
    _ensure_source_spans(conn)
    conn.commit()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _ensure_parent_dir(conn: sqlite3.Connection) -> None:
    """补 parent_dir（父目录名）字段 + 可入库清单视图（002）。"""
    if not _column_exists(conn, "source_documents", "parent_dir"):
        conn.execute("ALTER TABLE source_documents ADD COLUMN parent_dir TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_parent ON source_documents(parent_dir)")
    # 视图必须可重建：清单口径会演进（如"隔离区永不出现在清单里"），IF NOT EXISTS 改不动旧定义
    conn.execute("DROP VIEW IF EXISTS ingest_queue")
    conn.execute(
        """
        CREATE VIEW ingest_queue AS
        SELECT doc_hash, file_name, parent_dir, dir_path, ext, size_bytes,
               category, topic, sub_topic, collection, source_trust
        FROM source_documents
        WHERE status = 'classified'
          AND dir_path NOT LIKE '99-重复文件待删%'
        """
    )


def _ensure_source_spans(conn: sqlite3.Connection) -> None:
    """补合并版"源边界"字段（003）：source_count / sources_json（见 meta/schema/003）。"""
    for col, decl in (("source_count", "INTEGER"), ("sources_json", "TEXT")):
        if not _column_exists(conn, "source_documents", col):
            conn.execute(f"ALTER TABLE source_documents ADD COLUMN {col} {decl}")


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
