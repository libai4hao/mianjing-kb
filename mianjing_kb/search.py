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
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
RRF_K = 60


class VectorUnavailable(RuntimeError):
    """向量模型不可用（未下载/ONNX 初始化失败/推理异常），可安全降级到关键词。"""


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
_SELECT = ("SELECT q.qid, q.q_key, q.doc_hash, q.q_text, q.qtype, q.topic, q.difficulty, "
           "q.tags_json, q.position, q.quality, COALESCE(d.file_name, dc.md_rel) AS source, "
           "dc.md_rel AS source_path, q.a_text ")
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
            "WHERE questions_fts MATCH ?" + filt + " ORDER BY rank LIMIT ?",
            [fts_query, *params, limit]).fetchall()
        if not rows:
            rows = con.execute(
                _SELECT + "FROM questions q" + _JOIN +
                "WHERE (q.q_text LIKE ? OR q.a_text LIKE ?)" + filt +
                " ORDER BY q.qid LIMIT ?",
                [f"%{query}%", f"%{query}%", *params, limit]).fetchall()
    else:
        rows = con.execute(
            _SELECT + "FROM questions q" + _JOIN +
            "WHERE (q.q_text LIKE ? OR q.a_text LIKE ?)" + filt +
            " ORDER BY q.qid LIMIT ?",
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
    try:
        vec = embed_texts([query])[0]
    except Exception as ex:  # noqa: BLE001 —— 模型下载/ONNX/推理失败统一视为可降级
        raise VectorUnavailable(f"向量模型不可用：{type(ex).__name__}: {ex}") from ex
    import sqlite_vec
    meta = vec_metadata_ready(con)
    # 标量字段可下推；多选/标签只能在 KNN 后过滤。旧表没有 metadata 时，
    # topic/qtype/difficulty 也是后过滤，必须一起纳入过采样判断。
    post = bool(position or quality or tags
                or (not meta and (topic or qtype or difficulty is not None)))
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
                  tags=None, pool: int = 30, diagnostics: dict | None = None) -> list[dict]:
    kw = keyword_search(con, query, pool, topic, qtype, difficulty, position, quality, tags)
    if not has_vec_table(con):
        if diagnostics is not None:
            diagnostics["vector"] = "absent"
        return kw[:limit]
    try:
        vs = vector_search(con, query, pool, topic, qtype, difficulty, position, quality, tags)
    except VectorUnavailable as ex:
        # 模型/推理不可用 → 关键词降级；向量表损坏等其它错误不吞。
        if diagnostics is not None:
            diagnostics["vector"] = "unavailable"
            diagnostics["why"] = str(ex)
        return kw[:limit]
    if diagnostics is not None:
        diagnostics["vector"] = "ok"
    fused = rrf_fuse([r["qid"] for r in kw], [r["qid"] for r in vs])[:limit]
    by_qid = {r["qid"]: r for r in kw + vs}
    out = []
    for qid, score in fused:
        out.append({**by_qid[qid], "rrf": round(score, 5)})
    return out


def rag_contexts(con, query: str, limit: int = 6, topic: str | None = None,
                 qtype: str | None = None, difficulty: int | None = None,
                 position: str | None = None, quality: str | None = None,
                 tags=None, a_max: int = 800,
                 max_context_chars: int = 6000) -> dict:
    """P3-③：RAG 管线结构化输出——直接可作为 LLM prompt 的 context 组装体。

    D-35：先按 q_norm_hash/规范化题干做精确去重，再按 canonical_id 做同簇软去重；
    代表条目聚合来源。D-36：按检索排序分配全局字符预算，不平均截断所有答案。
    """
    if max_context_chars < 0:
        raise ValueError("max_context_chars 不能为负")
    diag: dict = {}
    # 去重会减少条数，先多取一些候选，保持去重后的排序不变。
    pool = max(limit, min(limit * 4, 24))
    rows = hybrid_search(con, query, pool, topic, qtype, difficulty, position, quality, tags,
                        diagnostics=diag)
    rows = [dict(r) for r in rows]
    qnorms = _qnorm_hashes(con, [r["qid"] for r in rows])
    for r in rows:
        r["_qnorm_hash"] = qnorms.get(r["qid"])
    rows = _dedupe_exact(rows)
    rows = _dedupe_clusters(con, rows)
    contexts = [_context_of(r, a_max) for r in rows][:limit]
    contexts, chars = _apply_context_budget(contexts, max_context_chars)
    return {"query": query, "diagnostics": diag, "context_chars": chars,
            "max_context_chars": max_context_chars, "contexts": contexts}


def _qnorm_hashes(con, qids: list[int]) -> dict[int, str]:
    if not qids:
        return {}
    try:
        rows = con.execute(
            "SELECT qid, q_norm_hash FROM questions WHERE qid IN (%s)"
            % ",".join("?" for _ in qids), qids).fetchall()
        return {int(r["qid"]): (r["q_norm_hash"] or "") for r in rows}
    except Exception:  # noqa: BLE001 —— 旧测试库/旧库没有 q_norm_hash 列
        return {}


def _exact_key(row: dict) -> str:
    topic = str(row.get("topic") or "")
    h = (row.get("_qnorm_hash") or "").strip()
    if h:
        return f"h:{h}|{topic}"
    text = re.sub(r"\s+", "", (row.get("q_text") or "")).lower()
    return f"t:{text}|{topic}"


def _source_values(row: dict) -> list[str]:
    out = []
    for s in (row.get("source"), row.get("source_path"), *(row.get("_sources") or [])):
        if s and str(s) not in out:
            out.append(str(s))
    return out


def _merge_sources(rep: dict, row: dict) -> None:
    rep["_sources"] = _source_values(rep) + [s for s in _source_values(row)
                                             if s not in _source_values(rep)]
    rep["_duplicates"] = int(rep.get("_duplicates", 1)) + int(row.get("_duplicates", 1))


def _dedupe_exact(rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        key = _exact_key(row)
        if key in seen:
            _merge_sources(seen[key], row)
            continue
        row["_duplicates"] = 1
        seen[key] = row
        out.append(row)
    return out


def _canonical_ids(con, rows: list[dict]) -> dict[str, str]:
    keys = [str(r.get("q_key") or "") for r in rows if r.get("q_key")]
    if not keys:
        return {}
    try:
        q = ("SELECT q_key, canonical_id FROM canonical_dependents "
             "WHERE q_key IN (%s)" % ",".join("?" for _ in keys))
        return {str(r["q_key"]): str(r["canonical_id"])
                for r in con.execute(q, keys).fetchall()}
    except Exception:  # noqa: BLE001 —— 旧库/测试库没有簇表
        return {}


def _answer_ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, re.sub(r"\s+", "", a),
                                   re.sub(r"\s+", "", b)).ratio()


def _dedupe_clusters(con, rows: list[dict]) -> list[dict]:
    canon = _canonical_ids(con, rows)
    out: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for row in rows:
        cid = canon.get(str(row.get("q_key") or ""))
        if not cid:
            out.append(row)
            continue
        group = groups.setdefault(cid, [])
        # 不同主题不得因聚类被合并；主题不同直接保留。
        if group and row.get("topic") != group[0].get("topic"):
            out.append(row)
            continue
        if not group:
            group.append(row)
            out.append(row)
        elif len(group) == 1 and _answer_ratio(
                group[0].get("a_text") or "", row.get("a_text") or "") < 0.55:
            group.append(row)          # 答案明显不同：最多保留两个有价值变体
            out.append(row)
        else:
            _merge_sources(group[0], row)
    return out


def _context_of(row: dict, a_max: int) -> dict:
    sources = _source_values(row)
    return {
        "qid": row["qid"], "q_key": row.get("q_key"),
        "doc_hash": row.get("doc_hash"),
        "question": (row.get("q_text") or "")[:300],
        "answer": (row.get("a_text") or "")[:a_max],
        "source": sources[0] if sources else None,
        "source_path": sources[0] if sources else None,
        "sources": sources,
        "topic": row.get("topic"), "qtype": row.get("qtype"),
        "difficulty": row.get("difficulty"), "tags": row.get("tags_json"),
        "position": row.get("position"), "quality": row.get("quality"),
        "duplicates": int(row.get("_duplicates", 1)),
    }


def _apply_context_budget(contexts: list[dict], budget: int) -> tuple[list[dict], int]:
    """按排序分配全局预算：先保证短答案，再把剩余预算给靠前的长答案。"""
    if not contexts:
        return [], 0
    if budget <= 0:
        return [], 0
    mins = []
    for c in contexts:
        q_len, a_len = len(c["question"]), len(c["answer"])
        mins.append(q_len + min(a_len, 120))
    selected: list[dict] = []
    used = 0
    for c, need in zip(contexts, mins):
        if used + need > budget:
            continue                      # 跳过放不下的；后面更短的仍有机会
        selected.append(c)
        used += need
    if not selected:
        return [], 0
    # 先把所有入选条目压到最小保留长度，再把剩余预算按排序追加给靠前条目。
    full = {id(c): c["answer"] for c in selected}
    for c in selected:
        c["answer"] = full[id(c)][:120]
        c["chars"] = len(c["question"]) + len(c["answer"])
    remaining = budget - sum(c["chars"] for c in selected)
    for c in selected:
        original = full[id(c)]
        add = min(max(0, len(original) - 120), remaining)
        if add:
            c["answer"] = original[:120 + add]
            c["chars"] = len(c["question"]) + len(c["answer"])
            remaining -= add
    return selected, sum(c["chars"] for c in selected)


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
    diag: dict = {}
    if args.mode == "kw":
        rows = keyword_search(con, query, args.limit, args.topic, args.qtype, **extra)
    elif args.mode == "vec":
        if not has_vec_table(con):
            print(json.dumps({"err": "vec_questions 不存在，先跑 python -m mianjing_kb.embed_questions"},
                             ensure_ascii=False))
            return 2
        try:
            rows = vector_search(con, query, args.limit, args.topic, args.qtype, **extra)
        except VectorUnavailable as ex:
            con.close()
            print(json.dumps({"err": str(ex), "hint": "用 --mode kw 或修复向量模型"},
                             ensure_ascii=False))
            return 2
    else:
        rows = hybrid_search(con, query, args.limit, args.topic, args.qtype,
                             diagnostics=diag, **extra)
    con.close()
    for r in rows:
        r["q_text"] = r["q_text"][:80]
        r["a_text"] = (r.get("a_text") or "")[:200]
    out = {"query": query, "mode": args.mode, "hits": len(rows), "rows": rows}
    if args.mode == "hybrid":
        out["diagnostics"] = diag
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
