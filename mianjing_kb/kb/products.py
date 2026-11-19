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
           indexed_at: str | None = None,
           preserve_input_fingerprint: bool = False) -> None:
    """写/更新一行产物；input_path 用于取 size/mtime（增量判据）。

    `preserve_input_fingerprint=True` 用于扫描映射阶段：如果该文档已经成功抽取，
    只更新映射元数据，不得提前覆盖上次成功抽取的 md_hash / size / mtime。否则
    `needs_work()` 会看到刚写入的新指纹，误判已修改的输入为未变化。新建/未抽取行
    仍可初始化指纹；真正抽取成功后由调用方用默认参数提交新指纹。
    """
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
        " md_rel=excluded.md_rel, "
        " md_hash=CASE WHEN ? AND document_products.extracted_at IS NOT NULL "
        "              THEN document_products.md_hash ELSE excluded.md_hash END, "
        " text_source=excluded.text_source, "
        " gate_verdict=COALESCE(excluded.gate_verdict, document_products.gate_verdict), "
        " questions_n=CASE WHEN ? IS NULL THEN document_products.questions_n "
        "                   ELSE excluded.questions_n END, "
        " input_size=CASE WHEN ? AND document_products.extracted_at IS NOT NULL "
        "                 THEN document_products.input_size "
        "                 ELSE COALESCE(excluded.input_size, document_products.input_size) END, "
        " input_mtime=CASE WHEN ? AND document_products.extracted_at IS NOT NULL "
        "                  THEN document_products.input_mtime "
        "                  ELSE COALESCE(excluded.input_mtime, document_products.input_mtime) END, "
        " extracted_at=COALESCE(excluded.extracted_at, document_products.extracted_at), "
        " indexed_at=COALESCE(excluded.indexed_at, document_products.indexed_at), "
        " updated_at=excluded.updated_at",
        (doc_hash, md_rel, md_hash, text_source, gate_verdict,
         questions_n if questions_n is not None else 0,
         size, mtime, extracted_at, indexed_at, _now(),
         int(preserve_input_fingerprint), questions_n,
         int(preserve_input_fingerprint), int(preserve_input_fingerprint)))


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
    if st.st_size != row["input_size"] or abs(st.st_mtime - row["input_mtime"]) > 1e-6:
        return True
    # 对 md/orphan 再用内容 hash 兜底：同大小、mtime 被保留/回拨时仍能识别内容变化。
    # 只在 stat 指纹相同的情况下读取文件，常见的未变化增量扫描不会多读大文件。
    if row["text_source"] in ("md", "orphan"):
        if not row["md_hash"]:
            return True
        try:
            return hashing.doc_hash(input_path) != row["md_hash"]
        except OSError:
            return False
    return False


def sync_mapping(conn, mapping) -> int:
    """把 mdmap 的映射先记成产物（extracted_at 留 NULL，等 extract 完成后回填）。

    返回写入行数。已存在的行不覆盖 extracted_at / questions_n（COALESCE 语义）。
    """
    n = 0
    for doc_hash, (rel, path) in mapping.by_doc.items():
        upsert(conn, doc_hash, md_rel=rel, md_hash=hashing.doc_hash(path),
               text_source="md", input_path=path,
               preserve_input_fingerprint=True)
        n += 1
    for mh, (rel, path) in mapping.orphans.items():
        upsert(conn, mh, md_rel=rel, md_hash=hashing.doc_hash(path),
               text_source="orphan", input_path=path,
               preserve_input_fingerprint=True)
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
