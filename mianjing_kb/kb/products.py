# -*- coding: utf-8 -*-
"""document_products：产物台账（Q-3 / P1-1，2026-11-19）。

为什么要有这张表：状态机在 `classified` 断路时，`extract` 只能靠扫盘和文件名猜"这篇
有没有 md、抽没抽过题"；增量接入也就无从判断。这里把"每篇的处理结果"落成事实：

- 一行一个输入身份：源 `doc_hash`，或孤儿 md 的 `md-mirror:` hash（P0-1 单身份）；
- `text_source` 记录题从哪抽：md / source / orphan；
- `input_size` / `input_mtime` 是增量判据（文件没变就跳过）；
- `questions_n` / `extracted_at` 让"0 题"也算处理完成，不会每次重试；
- 状态机只在成功后写 `processed`，失败留 `processing`（断点续跑依据）。

本模块只读写这张表，不碰 source_documents 状态（状态迁移在 extract 里做，便于审计）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mianjing_kb.kb import hashing

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(NOW_FMT)


def get(conn, doc_hash: str):
    return conn.execute("SELECT * FROM document_products WHERE doc_hash=?",
                        (doc_hash,)).fetchone()


def upsert(conn, doc_hash: str, *, md_rel: str | None, md_hash: str | None,
           text_source: str, input_path: Path | None, questions_n: int | None = None,
           gate_verdict: str | None = None, extracted_at: str | None = None,
           indexed_at: str | None = None) -> None:
    """写/更新一行产物；input_path 用于取 size/mtime（增量判据）。"""
    if text_source not in ("md", "source", "orphan"):
        raise ValueError(f"未知 text_source：{text_source}")
    size = mtime = None
    if input_path is not None:
        try:
            st = Path(input_path).stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            pass
    conn.execute(
        "INSERT INTO document_products "
        "(doc_hash, md_rel, md_hash, text_source, gate_verdict, questions_n, "
        " input_size, input_mtime, extracted_at, indexed_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_hash) DO UPDATE SET "
        " md_rel=excluded.md_rel, md_hash=excluded.md_hash, text_source=excluded.text_source, "
        " gate_verdict=COALESCE(excluded.gate_verdict, document_products.gate_verdict), "
        " questions_n=COALESCE(excluded.questions_n, document_products.questions_n), "
        " input_size=COALESCE(excluded.input_size, document_products.input_size), "
        " input_mtime=COALESCE(excluded.input_mtime, document_products.input_mtime), "
        " extracted_at=COALESCE(excluded.extracted_at, document_products.extracted_at), "
        " indexed_at=COALESCE(excluded.indexed_at, document_products.indexed_at), "
        " updated_at=excluded.updated_at",
        (doc_hash, md_rel, md_hash, text_source, gate_verdict, questions_n or 0,
         size, mtime, extracted_at, indexed_at, _now()))


def needs_work(conn, doc_hash: str, input_path: Path) -> bool:
    """增量判据：没有产物行 / 没抽过 / 输入文件大小或 mtime 变了 → 需要处理。"""
    row = get(conn, doc_hash)
    if row is None or row["extracted_at"] is None:
        return True
    try:
        st = Path(input_path).stat()
    except OSError:
        return False            # 文件没了，留给 G5 / purge 处理，不在这里报错
    if row["input_size"] is None or row["input_mtime"] is None:
        return True
    return st.st_size != row["input_size"] or abs(st.st_mtime - row["input_mtime"]) > 1e-6


def sync_mapping(conn, mapping) -> int:
    """把 mdmap 的映射先记成产物（extracted_at 留 NULL，等 extract 完成后回填）。

    返回写入行数。已存在的行不覆盖 extracted_at / questions_n（COALESCE 语义）。
    """
    n = 0
    for doc_hash, (rel, path) in mapping.by_doc.items():
        upsert(conn, doc_hash, md_rel=rel, md_hash=hashing.doc_hash(path),
               text_source="md", input_path=path)
        n += 1
    for mh, (rel, path) in mapping.orphans.items():
        upsert(conn, mh, md_rel=rel, md_hash=hashing.doc_hash(path),
               text_source="orphan", input_path=path)
        n += 1
    return n


def summary(conn) -> dict:
    rows = conn.execute(
        "SELECT text_source, COUNT(*) n, "
        " SUM(CASE WHEN extracted_at IS NOT NULL THEN 1 ELSE 0 END) extracted, "
        " SUM(CASE WHEN indexed_at   IS NOT NULL THEN 1 ELSE 0 END) indexed_n, "
        " SUM(questions_n) questions FROM document_products GROUP BY 1").fetchall()
    return {r["text_source"]: {"rows": r["n"], "extracted": r["extracted"],
                               "indexed": r["indexed_n"], "questions": r["questions"]}
            for r in rows}
