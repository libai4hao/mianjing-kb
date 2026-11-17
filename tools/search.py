# -*- coding: utf-8 -*-
"""P3-① search：面经库.db 混合检索（keyword FTS + 向量 KNN + RRF 融合）。

- kw：FTS5 trigram（查询串 ≥3 字符）/ LIKE 兜底（<3 字符），支持 topic/qtype 过滤
- vec：bge-small-zh-v1.5 (fastembed ONNX, 512维) 对 vec_questions 做 KNN——需先跑 embed_questions.py
- hybrid：双路各取 top n → RRF 融合 score=Σ 1/(60+rank)
用法：
    python tools/search.py "HashMap 扩容"                     # 默认 hybrid（无向量表自动降级 kw）
    python tools/search.py "端口" --topic MySQL --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config  # noqa: E402

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
        rows = con.execute(
            "SELECT q.qid, q.q_text, q.qtype, q.topic, d.file_name "
            "FROM questions_fts f JOIN questions q ON q.qid=f.rowid "
            "LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
            "WHERE questions_fts MATCH ?" + filt + " LIMIT ?",
            [query, *params, limit]).fetchall()
    else:
        rows = con.execute(
            "SELECT q.qid, q.q_text, q.qtype, q.topic, d.file_name "
            "FROM questions q LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
            "WHERE (q.q_text LIKE ? OR q.a_text LIKE ?)" + filt + " LIMIT ?",
            [f"%{query}%", f"%{query}%", *params, limit]).fetchall()
    return [dict(r) for r in rows]


def has_vec_table(con) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_questions'").fetchone()[0] > 0


def vector_search(con, query: str, limit: int = 10, topic: str | None = None,
                  qtype: str | None = None) -> list[dict]:
    from embed_questions import embed_texts  # 惰性加载模型
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
        "SELECT q.qid, q.q_text, q.qtype, q.topic, d.file_name, v.distance "
        "FROM vec_questions v JOIN questions q ON q.qid=v.rowid "
        "LEFT JOIN documents d ON d.doc_hash=q.doc_hash "
        "WHERE v.embedding MATCH ? AND k = ?" + filt + " ORDER BY distance",
        [sqlite_vec.serialize_float32(vec), int(limit), *params]).fetchall()
    return [dict(r) for r in rows]


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面经混合检索")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--mode", choices=["kw", "vec", "hybrid"], default="hybrid")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--qtype", default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--db", default=str(OUT_DB))
    args = ap.parse_args(argv)
    query = " ".join(args.query)
    con = _con(Path(args.db))
    if args.mode == "kw":
        rows = keyword_search(con, query, args.limit, args.topic, args.qtype)
    elif args.mode == "vec":
        if not has_vec_table(con):
            print(json.dumps({"err": "vec_questions 不存在，先跑 python tools/embed_questions.py"},
                             ensure_ascii=False))
            return 2
        rows = vector_search(con, query, args.limit, args.topic, args.qtype)
    else:
        rows = hybrid_search(con, query, args.limit, args.topic, args.qtype)
    con.close()
    for r in rows:
        r["q_text"] = r["q_text"][:80]
    print(json.dumps({"query": query, "mode": args.mode, "hits": len(rows), "rows": rows},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
