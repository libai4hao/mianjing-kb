# -*- coding: utf-8 -*-
"""检索评测：demo 查询集命中率 + Golden Query 分级指标（D-34）。

Golden Query 口径：
- 每条查询用 q_key 标注相关项及相关性等级（grade 0~3，缺省 1）；
- 指标：Recall@5、MRR@10、nDCG@10、无答案误命中率、分主题召回率；
- 检索策略/模型升级时跑全量 Golden Query，结果写入报告供 refresh 门禁人工复核。

用法：
    python -m mianjing_kb.eval_retrieval --queries eval/demo_queries.yaml --db <面经库.db> --k 5
    python -m mianjing_kb.eval_retrieval --golden eval/golden_queries.yaml --db <面经库.db> --mode kw
    python -m mianjing_kb.eval_retrieval --golden eval/golden_queries.yaml --report build/golden_report.json
退出码：Golden 模式下 Recall@5 低于 --min-recall 时返回 1；demo 模式沿用 hit@k。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config  # noqa: E402
from mianjing_kb import search as S  # noqa: E402


def load_queries(path: Path) -> list[dict]:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("queries") or []
    if not rows:
        raise ValueError(f"{path} 里没有 queries")
    return rows


def hit(row: dict, expects: list[str]) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in ("q_text", "a_text", "source"))
    return any(e in blob for e in expects)


def _search_kwargs(item: dict, k: int) -> dict:
    return {
        "limit": k,
        "topic": item.get("topic"),
        "qtype": item.get("qtype"),
        "difficulty": item.get("difficulty"),
        "position": item.get("position"),
        "quality": item.get("quality"),
        "tags": item.get("tags"),
    }


def evaluate(db: Path, queries: list[dict], k: int, mode: str) -> dict:
    """旧 demo 口径：expect_any / expect_ids 的 hit@k，保持向后兼容。"""
    con = S._con(db)
    fn = {"kw": S.keyword_search, "vec": S.vector_search, "hybrid": S.hybrid_search}[mode]
    details, hits, strict_hits = [], 0, 0
    try:
        for item in queries:
            q = item["q"]
            expects = item.get("expect_any") or []
            ids = item.get("expect_ids") or []
            rows = fn(con, q, **_search_kwargs(item, k))
            if expects:
                ok = any(hit(r, expects) for r in rows)
            elif ids:
                got = {str(r.get("doc_hash") or "") for r in rows}
                ok = any(i in got for i in ids)
            else:
                ok = bool(rows)     # 没给期望时只记"有没有返回"，用于冒烟，不代表质量
            hits += 1 if ok else 0
            if ids:
                strict_hits += 1 if any(i in {str(r.get("doc_hash") or "") for r in rows} for i in ids) else 0
            details.append({"q": q, "ok": ok, "top": (rows[0]["q_text"] if rows else None)})
    finally:
        con.close()                 # 异常路径也要释放连接（候选 #23 加固项）
    out = {"queries": len(queries), "k": k, "mode": mode,
           "hit_at_k": round(hits / len(queries), 4), "details": details}
    out["recall_at_k"] = out["hit_at_k"]      # 兼容旧字段名；只有给全相关项时才是严格 recall
    if any(item.get("expect_ids") for item in queries):
        n = sum(1 for item in queries if item.get("expect_ids"))
        out["strict_recall_at_k"] = round(strict_hits / n, 4)
    return out


def _qid(key: str) -> str:
    """q_key = <doc_hash>:<q_norm_hash>；同一归一题干跨文档应视为同一相关项。"""
    return str(key or "").split(":")[-1]


def _relevance(item: dict) -> dict[str, float]:
    """分级相关表：grade 必须是 0..3 的数值（schema 口径）；重复键拒绝。

    #21：grade 0 表示"不相关"，保留在表里供 nDCG 零增益，但不得给 recall 记分。
    """
    rel = {}
    for entry in item.get("relevance") or []:
        key = _qid(entry.get("q_key") or "")
        if not key:
            continue
        grade = entry.get("grade", 1)
        if isinstance(grade, bool) or not isinstance(grade, (int, float)) \
                or not 0 <= grade <= 3:
            raise ValueError(f"relevance grade 非法（须 0..3）: {key}={grade!r}")
        if key in rel:
            raise ValueError(f"relevance 重复键: {key}")
        rel[key] = float(grade)
    return rel


def _dcg(grades: list[float]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def _ndcg(ranked: list[str], rel: dict[str, float], k: int) -> float:
    gains = [rel.get(key, 0.0) for key in ranked[:k]]
    ideal = sorted(rel.values(), reverse=True)[:k]
    denom = _dcg(ideal)
    return _dcg(gains) / denom if denom else 0.0


def _one_query(con, item: dict, k: int, mode: str) -> dict:
    q = item["query"]
    fn = {"kw": S.keyword_search, "vec": S.vector_search, "hybrid": S.hybrid_search}[mode]
    rows = fn(con, q, **_search_kwargs(item, k))
    keys = [str(r.get("q_key") or "") for r in rows]
    # #20：同一归一题干跨文档是同一相关项——按身份去重（保首次出现序），
    # 否则重复身份在 DCG 里多次记分而 IDCG 只算一份，nDCG 会超过 1。
    ids = list(dict.fromkeys(_qid(key) for key in keys))
    rel = _relevance(item)
    expected = [_qid(x) for x in (item.get("expect_q_keys") or []) if x]
    if not rel and expected:
        rel = {key: 1.0 for key in expected}
    elif rel and expected:
        # #21：分级表是权威——expect_q_keys 只在无分级表时兜底；
        # 两表矛盾（expect 里的键分级为 0 或缺席）说明标注不一致，拒绝该条。
        bad = [k for k in expected if rel.get(k, 0) <= 0]
        if bad:
            raise ValueError(f"expect_q_keys 与 relevance 矛盾（grade 0/缺席）: {bad}")
    # recall 只认正分级（grade>0）；grade 0 的键不给召回记分
    relevant = {k for k, g in rel.items() if g > 0}
    top5 = set(ids[:5])
    recall5 = len(relevant & top5) / len(relevant) if relevant else None
    mrr10 = 0.0
    for i, key in enumerate(ids[:10], 1):
        if key in relevant:
            mrr10 = 1.0 / i
            break
    ndcg10 = _ndcg(ids, rel, 10) if relevant else None
    return {
        "query": q, "topic": item.get("topic"), "category": item.get("category"),
        # #22：只有布尔 True 才算已人工审核；"yes"/1 等 truthy 值不得满足 G9 前提
        "reviewed": item.get("reviewed") is True,
        "expected": sorted(relevant), "top_q_keys": keys[:10],
        "recall_at_5": recall5, "mrr_at_10": mrr10, "ndcg_at_10": ndcg10,
        "no_answer_false_positive": (bool(rows) if not relevant else None),
        "top_question": (rows[0].get("q_text") if rows else None),
    }


def evaluate_golden(db: Path, queries: list[dict], k: int = 10, mode: str = "kw") -> dict:
    con = S._con(db)
    try:
        details = [_one_query(con, item, k, mode) for item in queries]
    finally:
        con.close()
    answered = [d for d in details if d["recall_at_5"] is not None]
    no_answer = [d for d in details if d["no_answer_false_positive"] is not None]
    recalls = [d["recall_at_5"] for d in answered]
    mrr = [d["mrr_at_10"] for d in answered]
    ndcg = [d["ndcg_at_10"] for d in answered]
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
    topics: dict[str, list] = {}
    for d in answered:
        topics.setdefault(d.get("topic") or "未分类", []).append(d["recall_at_5"])
    per_topic = {t: {"queries": len(v), "recall_at_5": mean(v)} for t, v in topics.items()}
    return {
        "queries": len(queries), "k": k, "mode": mode,
        "recall_at_5": mean(recalls), "mrr_at_10": mean(mrr), "ndcg_at_10": mean(ndcg),
        "no_answer_false_positive_rate": mean(
            [1.0 if d["no_answer_false_positive"] else 0.0 for d in no_answer]),
        "per_topic_recall_at_5": per_topic,
        "reviewed_count": sum(1 for d in details if d.get("reviewed") is True),
        "details": details,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检索评测（demo hit@k / Golden Query 分级指标）")
    ap.add_argument("--queries", default=str(REPO / "eval" / "demo_queries.yaml"))
    ap.add_argument("--golden", default=None, help="Golden Query 文件；设置后启用 Recall/MRR/nDCG")
    ap.add_argument("--db", default=str(Path(config.BUILD_DIR) / "面经库.db"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--mode", choices=["kw", "hybrid", "vec"], default="kw")
    ap.add_argument("--min-recall", type=float, default=0.0, help="低于此值 exit 1")
    ap.add_argument("--report", default=None, help="Golden 报告 JSON 输出路径")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(json.dumps({"error": f"库不存在：{db}", "hint": "先跑 kb demo 或 kb build-db"},
                         ensure_ascii=False))
        return 2
    if args.golden:
        res = evaluate_golden(db, load_queries(Path(args.golden)), max(args.k, 10), args.mode)
        if args.report:
            out = Path(args.report)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=1))
        score = res["recall_at_5"] or 0.0
        return 0 if score >= args.min_recall else 1

    res = evaluate(db, load_queries(Path(args.queries)), args.k, args.mode)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["hit_at_k"] >= args.min_recall else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
