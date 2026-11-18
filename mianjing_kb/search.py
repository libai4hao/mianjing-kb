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
_SELECT = ("SELECT q.qid, q.q_text, q.qtype, q.topic, "
           "COALESCE(d.file_name, dc.md_rel) AS source, q.a_text ")


def keyword_search(con, query: str, limit: int = 10, topic: str | None = None,
                   qtype: str | None = None) -> list[dict]:
    filt = ""
    params: list = []
    if topic:
        filt += " AND q.topic=?"
        params.append(topic)
    if qtype:
        filt += " AND q.qtype=?"
        params.append(qtype)
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
                  qtype: str | None = None) -> list[dict]:
    from mianjing_kb.embed_questions import embed_texts  # 惰性加载模型
    filt = ""
    params: list = []
    if topic:
        filt += " AND q.topic=?"
        params.append(topic)
    if qtype:
        filt += " AND q.qtype=?"
        params.append(qtype)
    vec = embed_texts([query])[0]
    import sqlite_vec
    rows = con.execute(
        _SELECT + ", q.qid AS _qid, v.distance "
        "FROM vec_questions v JOIN questions q ON q.qid=v.rowid" + _JOIN +
        "WHERE v.embedding MATCH ? AND k = ?" + filt + " ORDER BY distance",
        [sqlite_vec.serialize_float32(vec), int(limit), *params]).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_qid"} for r in rows]


def rrf_fuse(rank_a: list[int], rank_b: list[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """两路排名融合：score = Σ 1/(k+rank)，rank 从 1 起。返回 [(qid, score)] 按 score 降序。"""
    scores: dict[int, float] = {}
    for rank, qid in enumerate(rank_a, 1):
        scores[qid] = scores.get(qid, 0.0) + 1.0 / (k + rank)
    for rank, qid in enumerate(rank_b, 1):
        scores[qid] = scores.get(qid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_search(con, query: str, limit: int = 10, topic: str | None = None,
                  qtype: str | None = None, pool: int = 30) -> list[dict]:
    kw = keyword_search(con, query, pool, topic, qtype)
    if not has_vec_table(con):
        return kw[:limit]
    vs = vector_search(con, query, pool, topic, qtype)
    fused = rrf_fuse([r["qid"] for r in kw], [r["qid"] for r in vs])[:limit]
    by_qid = {r["qid"]: r for r in kw + vs}
    out = []
    for qid, score in fused:
        out.append({**by_qid[qid], "rrf": round(score, 5)})
    return out


def rag_contexts(con, query: str, limit: int = 6, topic: str | None = None,
                 qtype: str | None = None, a_max: int = 800) -> dict:
    """P3-③：RAG 管线结构化输出——直接可作为 LLM prompt 的 context 组装体。"""
    rows = hybrid_search(con, query, limit, topic, qtype)
    return {"query": query, "contexts": [
        {"qid": r["qid"], "question": r["q_text"][:300], "answer": (r.get("a_text") or "")[:a_max],
         "source": r.get("source"), "topic": r["topic"], "qtype": r["qtype"]} for r in rows]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面经混合检索")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--mode", choices=["kw", "vec", "hybrid"], default="hybrid")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--qtype", default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--format", choices=["json", "rag"], default="json",
                    help="rag=面向 LLM 管线的 contexts 结构（hybrid）")
    ap.add_argument("--db", default=str(OUT_DB))
    args = ap.parse_args(argv)
    query = " ".join(args.query)
    con = _con(Path(args.db))
    if args.format == "rag":
        out = rag_contexts(con, query, args.limit, args.topic, args.qtype)
        con.close()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.mode == "kw":
        rows = keyword_search(con, query, args.limit, args.topic, args.qtype)
    elif args.mode == "vec":
        if not has_vec_table(con):
            print(json.dumps({"err": "vec_questions 不存在，先跑 python -m mianjing_kb.embed_questions"},
                             ensure_ascii=False))
            return 2
        rows = vector_search(con, query, args.limit, args.topic, args.qtype)
    else:
        rows = hybrid_search(con, query, args.limit, args.topic, args.qtype)
    con.close()
    for r in rows:
        r["q_text"] = r["q_text"][:80]
        r["a_text"] = (r.get("a_text") or "")[:200]
    print(json.dumps({"query": query, "mode": args.mode, "hits": len(rows), "rows": rows},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
