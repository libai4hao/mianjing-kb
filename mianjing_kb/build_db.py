# -*- coding: utf-8 -*-
"""P2-④ build_db：kb.sqlite（source_documents + questions）→ 面经库.db（检索库）。

- documents：classified 源文档（含 source_count/sources_json 书签源边界）
- questions：13.5k 题块（q/a/qtype/conf/topic + q_norm_hash 供一题多源聚合）
- doc_contents + docs_fts：20-题库MD 正文（trigram，中文子串检索免分词）
- questions_fts：题干 + 答案[:8000]（trigram）
- vec_questions：向量表由 embed_questions 建（sqlite-vec）；本脚本重建时**连带 DROP**，
  否则旧向量会按 qid 残留在新题表上（新题无向量、已删题留旧向量，检索静默少召回）。
  直跑本脚本后需要 `python -m mianjing_kb.embed_questions --reembed`（或直接走 refresh.py）。

用法：
    python -m mianjing_kb.build_db               # 全量重建（面经库.db，DROP+CREATE）
    python -m mianjing_kb.build_db --query 端口   # 建完顺手验证一条 2 字查询
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config, db, status  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
MD_ROOT = Path(config.CORPUS_ROOT) / "20-题库MD"


def _base_stem(stem: str) -> str:
    """剥同名冲突后缀：`面试题_a1b2c3d4`（reconvert 单下划线）与
    `面试题__a1b2c3d4`（backfill 双下划线）两种口径都要剥干净。
    双下划线必须先判且判两位——否则会被单下划线分支剥成 `面试题_` 留尾（2026-11-17 审核修复）。"""
    if len(stem) > 10 and stem[-10] == "_" and stem[-9] == "_" and \
            all(c in "0123456789abcdef" for c in stem[-8:]):
        return stem[:-10]
    if len(stem) > 9 and stem[-9] == "_" and \
            all(c in "0123456789abcdef" for c in stem[-8:]):
        return stem[:-9]
    return stem


def build(out_db: Path | None = None) -> dict:
    # 路径在调用时取 config（T3 改名后模块常量和默认参数会跨测试/跨进程冻结成旧值）
    out_db = Path(out_db or (Path(config.BUILD_DIR) / "面经库.db"))
    md_root = Path(config.CORPUS_ROOT) / "20-题库MD"
    src = db.connect(config.DB_PATH, create=False)
    started = time.perf_counter()
    out = sqlite3.connect(str(out_db))
    out.executescript("""
PRAGMA journal_mode=WAL;
DROP TABLE IF EXISTS documents; DROP TABLE IF EXISTS questions;
DROP TABLE IF EXISTS doc_contents;
DROP TABLE IF EXISTS questions_fts; DROP TABLE IF EXISTS docs_fts;
-- 向量表不在本脚本建，但不能留旧表：rowid=qid 与重建后的 questions 会错位（2026-11-19 审核修复）
DROP TABLE IF EXISTS vec_questions;
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
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT,
  mirror_hash TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS idx_dc_mirror ON doc_contents(mirror_hash);
CREATE INDEX IF NOT EXISTS idx_dc_doc ON doc_contents(doc_hash);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
CREATE VIRTUAL TABLE docs_fts USING fts5(file_name, content, tokenize='trigram');
""")
    # ---- documents：classified 源文档 ----
    docs = src.execute(
        "SELECT doc_hash, file_name, category, topic, sub_topic, collection, source_trust, "
        "       dir_path, source_count, sources_json, note FROM source_documents "
        f"WHERE status='{status.CLASSIFIED}'").fetchall()
    out.executemany(
        "INSERT INTO documents(doc_hash, file_name, category, topic, sub_topic, collection, "
        "  source_trust, dir_path, source_count, sources_json, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(r) for r in docs])
    # ---- doc_contents：20-题库MD 正文（检索原文）——先于 questions 落库 ----
    # G3/K12 修复（docs/15，2026-11-17）：镜像题（doc_hash=sha256("md-mirror:"+rel)[:16]，
    # 无台账行）的 topic 此前落空串（生产 5,192 条），按继承链补齐：
    #   源文档 topic →（无源行时）md_rel 倒数第二段目录（转换约定 parent=topic，docs/14 K11 目录推断）
    by_stem = {}
    for r in docs:
        # 归一：台账文件名可能带尾随空格 / 大小写差异（md 侧 safe_name 已 strip）
        by_stem.setdefault(Path(r["file_name"]).stem.strip().lower(), r["doc_hash"])
    mirror_topic: dict[str, str] = {}
    mirror_cat: dict[str, str] = {}
    n_docs_content = 0
    md_paths = sorted(md_root.rglob("*.md")) if md_root.exists() else []
    import re
    _pat = re.compile(r"(?:__|_)[0-9a-f]{8}$")
    _hashed = {(p.parent, _pat.sub("", p.stem)) for p in md_paths if _pat.search(p.stem)}
    for p in md_paths:
        # 旧 x.md 与 reconvert 生成的 x_<hash8>.md 同目录共存时只收新版，避免同一原文双份
        if not _pat.search(p.stem) and (p.parent, p.stem) in _hashed:
            continue
        rel = p.relative_to(md_root).as_posix()
        stem = Path(rel).stem
        base = _base_stem(stem).strip().lower()
        text = p.read_text(encoding="utf-8", errors="replace")
        # P3-③：镜像 hash 回链——md 镜像题的 doc_hash = sha256("md-mirror:"+rel)[:16]
        mirror_hash = hashlib.sha256(("md-mirror:" + rel).encode("utf-8")).hexdigest()[:16]
        parts = rel.split("/")
        # F10（2026-11-19）：<category>/<topic>/<stem>.md 三段才有 topic 段；两段路径
        # parts[-2]==parts[0]==category，原 len>=2 会把 category 误当 topic。
        t_guess = parts[-2] if len(parts) >= 3 else ""
        c_guess = parts[0] if len(parts) >= 2 else ""
        mirror_topic[mirror_hash] = t_guess
        mirror_cat[mirror_hash] = c_guess
        doc_hash = by_stem.get(base) or by_stem.get(stem)
        if doc_hash:                                     # 源件 topic 空时同样走目录推断兜底
            mirror_topic.setdefault(doc_hash, t_guess)
            mirror_cat.setdefault(doc_hash, c_guess)
        out.execute("INSERT INTO doc_contents(md_rel, doc_hash, mirror_hash, content) VALUES(?,?,?,?)",
                    (rel, doc_hash, mirror_hash, text[:200000]))
        out.execute("INSERT INTO docs_fts(rowid, file_name, content) VALUES(?,?,?)",
                    (out.execute("SELECT last_insert_rowid()").fetchone()[0], stem, text[:100000]))
        n_docs_content += 1
    # ---- questions ----
    # 题级 topic 优先（006 列，batch_label merge 的事实源落点）→ 文档级 → 镜像继承兜底
    qs = src.execute(
        "SELECT q.qid, q.doc_hash, q.seq, q.q_text, q.a_text, q.qtype, q.conf, q.q_norm_hash, "
        "       COALESCE(q.topic, sd.topic, ''), COALESCE(sd.category, '') "
        "FROM questions q LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash").fetchall()
    out.executemany(
        "INSERT INTO questions(qid, doc_hash, seq, q_text, a_text, qtype, conf, "
        "  q_norm_hash, topic, category) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], (r[4] or "")[:8000], r[5], r[6], r[7],
          r[8] or mirror_topic.get(r[1], ""), r[9] or mirror_cat.get(r[1], "")) for r in qs])
    out.executemany(
        "INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(?,?,?)",
        [(r[0], r[3], (r[4] or "")[:8000]) for r in qs])
    out.commit()
    n_doc = out.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_q = out.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    elapsed = round(time.perf_counter() - started, 1)
    out.close()                     # 先关再量体积：WAL 未 checkpoint 时主文件会偏小
    size_mb = round(out_db.stat().st_size / 1e6, 1)
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
