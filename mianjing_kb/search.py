# -*- coding: utf-8 -*-
"""P3-① search：面经库.db 混合检索（keyword FTS + 向量 KNN + RRF 融合）。

- kw：FTS5 trigram（查询串 ≥3 字符）/ LIKE 兜底（<3 字符），支持 topic/qtype 过滤
- vec：bge-small-zh-v1.5 (fastembed ONNX, 512维) 对 vec_questions 做 KNN——需先跑 embed_questions.py
- hybrid：双路各取 top n → RRF 融合 score=Σ 1/(60+rank)
用法：
    python -m mianjing_kb.search "HashMap 扩容"                     # 默认 hybrid（无向量表自动降级 kw）
    python -m mianjing_kb.search "端口" --topic MySQL --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
RRF_K = 60


def _con(db_path: Path):
    import sqlite3
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:  # vec0 虚表需要扩展；文件库无向量表时跳过
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    except ImportError:
        pass
    return con


_JOIN = (
    " LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
    " LEFT JOIN doc_contents dc ON dc.mirror_hash=q.doc_hash "
)
_SELECT = ("SELECT q.qid, q.q_key, q.q_text, q.qtype, q.topic, q.difficulty, q.tags_json, "
           "q.position, q.quality, COALESCE(d.file_name, dc.md_rel) AS source, q.a_text ")
VEC_OVERSAMPLE = 4          # 多选/标签只能在 KNN 后过滤，先过采样再截断
VEC_MAX_CANDIDATES = 512    # 过采样上限，防止极端过滤把 k 放大到全表


def _tag_values(tags) -> list[str]:
    vals = [tags] if isinstance(tags, str) else list(tags or [])
    return [str(t).strip().lower() for t in vals if str(t).strip()]


def _sql_filters(topic: str | None = None, qtype: str | None = None,
                 difficulty: int | None = None, position: str | None = None,
                 quality: str | None = None, tags=None) -> tuple[str, list]:
    """questions 侧过滤 SQL（alias=q）。数组字段用 json_each，标签走 question_tags。

    这些条件在 vec 查询里是 KNN 之后的 SQL 过滤，所以调用方要先过采样；
    topic/qtype/difficulty 在 vec metadata 可用时优先下推到 KNN。
    """
    filt, params = "", []
    if topic:
        filt += " AND q.topic=?"
        params.append(topic)
    if qtype:
        filt += " AND q.qtype=?"
        params.append(qtype)
    if difficulty is not None:
        filt += " AND q.difficulty=?"
        params.append(int(difficulty))
    if position:
        filt += " AND EXISTS (SELECT 1 FROM json_each(q.position) WHERE value=?)"
        params.append(position)
    if quality:
        filt += " AND EXISTS (SELECT 1 FROM json_each(q.quality) WHERE value=?)"
        params.append(quality)
    for tag in _tag_values(tags):
        filt += (" AND EXISTS (SELECT 1 FROM question_tags qt "
                 "WHERE qt.qid=q.qid AND qt.tag_norm=?)")
        params.append(tag)
    return filt, params


def keyword_search(con, query: str, limit: int = 10, topic: str | None = None,
                   qtype: str | None = None, difficulty: int | None = None,
                   position: str | None = None, quality: str | None = None,
                   tags=None) -> list[dict]:
    filt, params = _sql_filters(topic, qtype, difficulty, position, quality, tags)
    if len(query) >= 3:
        # FTS5 MATCH 语法敏感（? " 等会炸），trigram 下用引号短语查询最稳：
        # 原文任意串 → "..." 字面匹配，内部双引号双写转义；空结果回退 LIKE。
        fts_query = '"' + query.replace('"', '""') + '"'
        rows = con.execute(
            _SELECT + "FROM questions_fts f JOIN questions q ON q.qid=f.rowid" + _JOIN +
            "WHERE questions_fts MATCH ?" + filt + " LIMIT ?",
            [fts_query, *params, limit]).fetchall()
        if not rows:
            rows = con.execute(
                _SELECT + "FROM questions q" + _JOIN +
                "WHERE (q.q_text LIKE ? OR q.a_text LIKE ?)" + filt + " LIMIT ?",
                [f"%{query}%", f"%{query}%", *params, limit]).fetchall()
    else:
        rows = con.execute(
            _SELECT + "FROM questions q" + _JOIN +
            "WHERE (q.q_text LIKE ? OR q.a_text LIKE ?)" + filt + " LIMIT ?",
            [f"%{query}%", f"%{query}%", *params, limit]).fetchall()
    return [dict(r) for r in rows]


def has_vec_table(con) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_questions'").fetchone()[0] > 0


def vector_search(con, query: str, limit: int = 10, topic: str | None = None,
                  qtype: str | None = None, difficulty: int | None = None,
                  position: str | None = None, quality: str | None = None,
                  tags=None) -> list[dict]:
    from mianjing_kb.embed_questions import embed_texts, vec_metadata_ready  # 惰性加载模型
    vec = embed_texts([query])[0]
    import sqlite_vec
    meta = vec_metadata_ready(con)
    # 标量字段可下推；多选/标签只能在 KNN 后过滤，所以先算出是否存在后过滤条件。
    post = bool(position or quality or tags)
    filt, params = _sql_filters(
        None if meta else topic, None if meta else qtype,
        None if meta else difficulty, position, quality, tags)
    meta_filt, meta_params = "", []
    if meta:
        if topic:
            meta_filt += " AND v.topic=?"
            meta_params.append(topic)
        if qtype:
            meta_filt += " AND v.qtype=?"
            meta_params.append(qtype)
        if difficulty is not None:
            meta_filt += " AND v.difficulty=?"
            meta_params.append(int(difficulty))

    def run(k: int) -> list[dict]:
        rows = con.execute(
            _SELECT + ", q.qid AS _qid, v.distance "
            "FROM vec_questions v JOIN questions q ON q.qid=v.rowid" + _JOIN +
            "WHERE v.embedding MATCH ? AND k = ?" + meta_filt + filt + " ORDER BY distance",
            [sqlite_vec.serialize_float32(vec), int(k), *meta_params, *params]).fetchall()
        return [{k2: v2 for k2, v2 in dict(r).items() if k2 != "_qid"} for r in rows]

    # 只有后过滤时才放大候选；没有后过滤时 k=limit 就是精确结果。
    k = int(limit) if not post else max(int(limit) * VEC_OVERSAMPLE, int(limit))
    rows = run(k)
    while post and len(rows) < limit and k < VEC_MAX_CANDIDATES:
        k = min(k * 2, VEC_MAX_CANDIDATES)
        rows = run(k)
    return rows[:limit]


def rrf_fuse(rank_a: list[int], rank_b: list[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """两路排名融合：score = Σ 1/(k+rank)，rank 从 1 起。返回 [(qid, score)] 按 score 降序。"""
    scores: dict[int, float] = {}
    for rank, qid in enumerate(rank_a, 1):
        scores[qid] = scores.get(qid, 0.0) + 1.0 / (k + rank)
    for rank, qid in enumerate(rank_b, 1):
        scores[qid] = scores.get(qid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_search(con, query: str, limit: int = 10, topic: str | None = None,
                  qtype: str | None = None, difficulty: int | None = None,
                  position: str | None = None, quality: str | None = None,
                  tags=None, pool: int = 30) -> list[dict]:
    kw = keyword_search(con, query, pool, topic, qtype, difficulty, position, quality, tags)
    if not has_vec_table(con):
        return kw[:limit]
    vs = vector_search(con, query, pool, topic, qtype, difficulty, position, quality, tags)
    fused = rrf_fuse([r["qid"] for r in kw], [r["qid"] for r in vs])[:limit]
    by_qid = {r["qid"]: r for r in kw + vs}
    out = []
    for qid, score in fused:
        out.append({**by_qid[qid], "rrf": round(score, 5)})
    return out


def rag_contexts(con, query: str, limit: int = 6, topic: str | None = None,
                 qtype: str | None = None, difficulty: int | None = None,
                 position: str | None = None, quality: str | None = None,
                 tags=None, a_max: int = 800) -> dict:
    """P3-③：RAG 管线结构化输出——直接可作为 LLM prompt 的 context 组装体。"""
    rows = hybrid_search(con, query, limit, topic, qtype, difficulty, position, quality, tags)
    return {"query": query, "contexts": [
        {"qid": r["qid"], "question": r["q_text"][:300], "answer": (r.get("a_text") or "")[:a_max],
         "source": r.get("source"), "topic": r["topic"], "qtype": r["qtype"],
         "difficulty": r.get("difficulty"), "tags": r.get("tags_json"),
         "position": r.get("position"), "quality": r.get("quality")} for r in rows]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面经混合检索")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--mode", choices=["kw", "vec", "hybrid"], default="hybrid")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--qtype", default=None)
    ap.add_argument("--difficulty", type=int, default=None, help="1-5 难度等值过滤")
    ap.add_argument("--position", default=None, help="岗位（position JSON 数组任一命中）")
    ap.add_argument("--quality", default=None, help="质量标记（quality JSON 数组任一命中）")
    ap.add_argument("--tag", action="append", default=None,
                    help="标签，可重复；多个 tag 为 AND（题必须同时带这些标签）")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--format", choices=["json", "rag"], default="json",
                    help="rag=面向 LLM 管线的 contexts 结构（hybrid）")
    ap.add_argument("--db", default=str(OUT_DB))
    args = ap.parse_args(argv)
    query = " ".join(args.query)
    con = _con(Path(args.db))
    extra = dict(difficulty=args.difficulty, position=args.position,
                 quality=args.quality, tags=args.tag)
    if args.format == "rag":
        out = rag_contexts(con, query, args.limit, args.topic, args.qtype, **extra)
        con.close()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.mode == "kw":
        rows = keyword_search(con, query, args.limit, args.topic, args.qtype, **extra)
    elif args.mode == "vec":
        if not has_vec_table(con):
            print(json.dumps({"err": "vec_questions 不存在，先跑 python -m mianjing_kb.embed_questions"},
                             ensure_ascii=False))
            return 2
        rows = vector_search(con, query, args.limit, args.topic, args.qtype, **extra)
    else:
        rows = hybrid_search(con, query, args.limit, args.topic, args.qtype, **extra)
    con.close()
    for r in rows:
        r["q_text"] = r["q_text"][:80]
        r["a_text"] = (r.get("a_text") or "")[:200]
    print(json.dumps({"query": query, "mode": args.mode, "hits": len(rows), "rows": rows},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
