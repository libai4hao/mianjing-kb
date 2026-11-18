# -*- coding: utf-8 -*-
"""检索评测：给定查询集与期望命中，算 recall@k，为分块策略 / 向量模型改动提供对比基线。

用法：
    python tools/eval_retrieval.py --queries eval/demo_queries.yaml --db <面经库.db> --k 5
    python tools/eval_retrieval.py --queries eval/demo_queries.yaml --mode kw --min-recall 0.8

查询集格式（yaml）：
    queries:
      - q: "HashMap 扩容"
        expect_any: ["扩容"]          # 命中判定：任一返回行的 q_text/a_text/source 含该子串
        topic: "Java"                 # 可选过滤
退出码：recall@k 低于 --min-recall 时返回 1（可直接挂 CI）。
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
import search as S  # noqa: E402


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
    details, hits = [], 0
    for item in queries:
        q = item["q"]
        expects = item.get("expect_any") or []
        rows = fn(con, q, limit=k, topic=item.get("topic"))
        ok = any(hit(r, expects) for r in rows) if expects else bool(rows)
        hits += 1 if ok else 0
        details.append({"q": q, "ok": ok, "top": (rows[0]["q_text"] if rows else None)})
    con.close()
    return {"queries": len(queries), "k": k, "mode": mode,
            "recall_at_k": round(hits / len(queries), 4), "details": details}


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
    return 0 if res["recall_at_k"] >= args.min_recall else 1


if __name__ == "__main__":
    raise SystemExit(main())
