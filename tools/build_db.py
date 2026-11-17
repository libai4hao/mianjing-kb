# -*- coding: utf-8 -*-
"""P2-④ build_db：kb.sqlite（source_documents + questions）→ 面经库.db（检索库）。

- documents：classified 源文档（含 source_count/sources_json 书签源边界）
- questions：13.5k 题块（q/a/qtype/conf/topic + q_norm_hash 供一题多源聚合）
- doc_contents + docs_fts：20-题库MD 正文（trigram，中文子串检索免分词）
- questions_fts：题干 + 答案[:8000]（trigram）
- 向量列：sqlite-vec / embedding 属 P3（模型未装），本轮不建，表结构留 doc_hash/topic 可回填

用法：
    python tools/build_db.py               # 全量重建（面经库.db，DROP+CREATE）
    python tools/build_db.py --query 端口   # 建完顺手验证一条 2 字查询
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config, db  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
MD_ROOT = Path(config.CORPUS_ROOT) / "20-题库MD"


def _base_stem(stem: str) -> str:
    """` stem_<hash8>` 同名冲突后缀剥壳：`面试题_a1b2c3d4` → `面试题`。"""
    if len(stem) > 9 and stem[-9] == "_" and all(c in "0123456789abcdef" for c in stem[-8:]):
        return stem[:-9]
    return stem


def build(out_db: Path = OUT_DB) -> dict:
    src = db.connect(config.DB_PATH, create=False)
    started = time.perf_counter()
    out = sqlite3.connect(str(out_db))
    out.executescript("""
PRAGMA journal_mode=WAL;
DROP TABLE IF EXISTS documents; DROP TABLE IF EXISTS questions;
DROP TABLE IF EXISTS doc_contents;
DROP TABLE IF EXISTS questions_fts; DROP TABLE IF EXISTS docs_fts;
CREATE TABLE documents(
  doc_hash TEXT PRIMARY KEY, file_name TEXT, category TEXT, topic TEXT, sub_topic TEXT,
  collection TEXT, source_trust TEXT, dir_path TEXT,
  source_count INTEGER, sources_json TEXT, note TEXT);
CREATE TABLE questions(
  qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER,
  q_text TEXT, a_text TEXT, qtype TEXT, conf TEXT,
  q_norm_hash TEXT, topic TEXT, category TEXT);
CREATE INDEX IF NOT EXISTS idx_qdoc ON questions(doc_hash);
CREATE INDEX IF NOT EXISTS idx_qnorm ON questions(q_norm_hash);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, content TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
CREATE VIRTUAL TABLE docs_fts USING fts5(file_name, content, tokenize='trigram');
""")
    # ---- documents：classified 源文档 ----
    docs = src.execute(
        "SELECT doc_hash, file_name, category, topic, sub_topic, collection, source_trust, "
        "       dir_path, source_count, sources_json, note FROM source_documents "
        "WHERE status='classified'").fetchall()
    out.executemany(
        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(r) for r in docs])
    # ---- questions ----
    qs = src.execute(
        "SELECT q.qid, q.doc_hash, q.seq, q.q_text, q.a_text, q.qtype, q.conf, q.q_norm_hash, "
        "       COALESCE(sd.topic,''), COALESCE(sd.category,'') "
        "FROM questions q LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash").fetchall()
    out.executemany(
        "INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4][:8000], r[5], r[6], r[7], r[8], r[9]) for r in qs])
    out.executemany(
        "INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(?,?,?)",
        [(r[0], r[3], r[4][:8000]) for r in qs])
    # ---- doc_contents：20-题库MD 正文（检索原文） ----
    # md 与源文档的对应：转换口径是 stem 同名（同名冲突件带 doc_hash 后缀），这里按 stem 关联 best-effort
    by_stem = {}
    for r in docs:
        by_stem.setdefault(Path(r["file_name"]).stem, r["doc_hash"])
    n_docs_content = 0
    for p in sorted(MD_ROOT.rglob("*.md")):
        rel = p.relative_to(MD_ROOT).as_posix()
        stem = Path(rel).stem
        base = _base_stem(stem)
        text = p.read_text(encoding="utf-8", errors="replace")
        out.execute("INSERT INTO doc_contents(md_rel, doc_hash, content) VALUES(?,?,?)",
                    (rel, by_stem.get(base) or by_stem.get(stem), text[:200000]))
        out.execute("INSERT INTO docs_fts(rowid, file_name, content) VALUES(?,?,?)",
                    (out.execute("SELECT last_insert_rowid()").fetchone()[0], stem, text[:100000]))
        n_docs_content += 1
    out.commit()
    n_doc = out.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_q = out.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    elapsed = round(time.perf_counter() - started, 1)
    size_mb = round(out_db.stat().st_size / 1e6, 1)
    out.close()
    src.close()
    return {"documents": n_doc, "questions": n_q, "doc_contents": n_docs_content,
            "size_mb": size_mb, "elapsed_sec": elapsed, "db": str(out_db)}


def demo_query(out_db: Path, keyword: str, limit: int = 5) -> list[dict]:
    con = sqlite3.connect(str(out_db))
    con.row_factory = sqlite3.Row
    if len(keyword) < 3:
        # FTS5 trigram 硬限制：查询串必须 ≥3 字符；2 字查询走 LIKE 兜底（13.5k 行全扫 ~ms 级）
        rows = con.execute(
            "SELECT q.qid, q.q_text, q.qtype, q.topic, d.file_name "
            "FROM questions q LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
            "WHERE q.q_text LIKE ? OR q.a_text LIKE ? LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT q.qid, q.q_text, q.qtype, q.topic, d.file_name "
            "FROM questions_fts f JOIN questions q ON q.qid=f.rowid "
            "LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
            "WHERE questions_fts MATCH ? ORDER BY rank LIMIT ?", (keyword, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="建检索库 面经库.db")
    ap.add_argument("--query", default=None, help="建完后验证一条 FTS 查询")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    stats = build()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    if args.query:
        hits = demo_query(OUT_DB, args.query, args.limit)
        print(json.dumps({"query": args.query, "hits": len(hits), "rows": hits},
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
