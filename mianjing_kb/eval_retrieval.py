# -*- coding: utf-8 -*-
"""检索评测：给定查询集与期望片段，算命中率@k，为分块策略 / 向量模型改动提供对比基线。

注意口径：这里的 `hit_at_k` 是"前 k 条里是否出现期望片段"，属于 hit@k；
真正的 recall@k 需要为每条查询标注**全部**相关文档，当前用 expect_any 近似。
所以它能抓"检索完全召回不到"的退化，抓不到"相关项还在但排名下滑"。

用法：
    python -m mianjing_kb.eval_retrieval --queries eval/demo_queries.yaml --db <面经库.db> --k 5
    python -m mianjing_kb.eval_retrieval --queries eval/demo_queries.yaml --mode kw --min-recall 0.8

查询集格式（yaml）：
    queries:
      - q: "HashMap 扩容"
        expect_any: ["扩容"]          # 命中判定：任一返回行的 q_text/a_text/source 含该子串
        topic: "Java"                 # 可选过滤
        expect_ids: ["<doc_hash>"]    # 可选：给全相关项后可算严格 recall@k
退出码：hit@k 低于 --min-recall 时返回 1（可直接挂 CI）。
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


def evaluate(db: Path, queries: list[dict], k: int, mode: str) -> dict:
    con = S._con(db)
    fn = {"kw": S.keyword_search, "vec": S.vector_search, "hybrid": S.hybrid_search}[mode]
    details, hits, strict_hits = [], 0, 0
    for item in queries:
        q = item["q"]
        expects = item.get("expect_any") or []
        ids = item.get("expect_ids") or []
        rows = fn(con, q, limit=k, topic=item.get("topic"))
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
    con.close()
    out = {"queries": len(queries), "k": k, "mode": mode,
           "hit_at_k": round(hits / len(queries), 4), "details": details}
    out["recall_at_k"] = out["hit_at_k"]      # 兼容旧字段名；只有给全相关项时才是严格 recall
    if any(item.get("expect_ids") for item in queries):
        n = sum(1 for item in queries if item.get("expect_ids"))
        out["strict_recall_at_k"] = round(strict_hits / n, 4)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检索评测（recall@k）")
    ap.add_argument("--queries", default=str(REPO / "eval" / "demo_queries.yaml"))
    ap.add_argument("--db", default=str(Path(config.BUILD_DIR) / "面经库.db"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--mode", choices=["kw", "hybrid", "vec"], default="kw")
    ap.add_argument("--min-recall", type=float, default=0.0, help="低于此值 exit 1")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(json.dumps({"error": f"库不存在：{db}", "hint": "先跑 kb demo 或 kb build-db"},
                         ensure_ascii=False))
        return 2
    res = evaluate(db, load_queries(Path(args.queries)), args.k, args.mode)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["hit_at_k"] >= args.min_recall else 1


if __name__ == "__main__":
    raise SystemExit(main())
