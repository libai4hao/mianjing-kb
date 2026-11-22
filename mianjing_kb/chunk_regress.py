# -*- coding: utf-8 -*-
"""切题回归基线（docs/47 P0-3）：固定一批文档，改 `chunker` 前后跑同一组数。

问题：过去 7 天 18 次 `fix(chunker)`，每次只能"重跑全量看总数"，既贵又不可比；
一次盲重跑 `chunk_v2` 还把题数从 19,913 推到 36,515（docs/44 的反例）。

做法：锁 20 个 doc_hash（8 个目录噪声 / 6 个切块错位 / 6 个正常），只存 hash 与计数量，
不存原文，公开仓库可入库。改 chunker 后先跑：

    python -m mianjing_kb.chunk_regress --check          # 对比基线，超容差即 FAIL
    python -m mianjing_kb.chunk_regress --make           # 有意改变行为后重建基线

计量四项：题数、空答案、**弱题**（不满足 `_heading_questionish` 的题，即"章节标题/碎句被当题"）、
probe 数。容差（默认）：单篇题数 ±5%，总题数 ±2%，空答案总数 ±2%，弱题只许减不许增。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import chunker  # noqa: E402
from mianjing_kb.kb import config  # noqa: E402

DOC_TOL = 0.05          # 单篇题数相对容差
TOTAL_TOL = 0.02        # 总题数相对容差
EMPTY_TOL = 0.02        # 空答案总数相对容差
WEAK_ABS = 0            # 弱题只许减不许增
KEYS = ("questions", "empty_answers", "weak_questions", "probe")


def measure(md_text: str) -> dict:
    """一篇文档的回归计量：题/空答案/弱题/probe。"""
    v2 = chunker.chunk_v2(md_text)
    items = v2.get("items") or []
    weak = sum(1 for it in items if not chunker._heading_questionish(it["q"]))
    return {
        "questions": len(items),
        "empty_answers": sum(1 for it in items if not (it.get("a") or "").strip()),
        "weak_questions": weak,
        "probe": len(v2.get("probe") or []),
    }


def resolve_text(conn, corpus_root: Path, doc_hash: str) -> str | None:
    """doc_hash → markdown 文本；md 镜像在 `20-题库MD/` 下（老库也有直接挂语料根的老数据）。"""
    row = conn.execute(
        "SELECT md_rel FROM document_products WHERE doc_hash=? AND md_rel IS NOT NULL",
        (doc_hash,)).fetchone()
    if not row or not row[0]:
        return None
    rel = str(row[0])
    for base in (corpus_root / "20-题库MD", corpus_root):
        path = base / rel
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    return None


def run(doc_hashes: list[str], db_path: Path, corpus_root: Path) -> dict:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    per = {}
    try:
        for h in doc_hashes:
            text = resolve_text(conn, corpus_root, h)
            per[h] = {"missing": True} if text is None else measure(text)
    finally:
        conn.close()
    total = {k: sum(v.get(k, 0) for v in per.values() if not v.get("missing")) for k in KEYS}
    return {"docs": len(doc_hashes),
            "missing": sum(1 for v in per.values() if v.get("missing")),
            "total": total, "per_doc": per}


def compare(cur: dict, base: dict) -> dict:
    """按容差判 PASS/FAIL；返回可读的违规项。"""
    fails = []
    b_total = base.get("total") or {}
    c_total = cur.get("total") or {}
    for key, tol in (("questions", TOTAL_TOL), ("empty_answers", EMPTY_TOL)):
        b, c = b_total.get(key, 0), c_total.get(key, 0)
        limit = max(1, int(round(b * tol)))
        if abs(c - b) > limit:
            fails.append(f"{key}: {b} → {c}（容差 ±{limit}）")
    b, c = b_total.get("weak_questions", 0), c_total.get("weak_questions", 0)
    if c - b > WEAK_ABS:
        fails.append(f"weak_questions: {b} → {c}（弱题只许减不许增）")
    for h, base_doc in (base.get("per_doc") or {}).items():
        if base_doc.get("missing"):
            continue
        cur_doc = (cur.get("per_doc") or {}).get(h) or {}
        if cur_doc.get("missing"):
            fails.append(f"{h}: 镜像缺失（基线有、现在读不到）")
            continue
        b, c = base_doc.get("questions", 0), cur_doc.get("questions", 0)
        limit = max(1, int(round(b * DOC_TOL)))
        if abs(c - b) > limit:
            fails.append(f"{h}: 题数 {b} → {c}（单篇容差 ±{limit}）")
    return {"verdict": "FAIL" if fails else "PASS", "fails": fails}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="切题回归基线：固定 doc_hash 清单，改 chunker 前后比数")
    ap.add_argument("--docs", default="eval/chunk_regress/docs.txt", help="每行一个 doc_hash")
    ap.add_argument("--baseline", default="eval/chunk_regress/baseline.json")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--corpus-root", default=str(config.CORPUS_ROOT))
    ap.add_argument("--make", action="store_true", help="重建基线（有意改变行为后才用）")
    ap.add_argument("--check", action="store_true", help="对比基线（默认行为）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    docs_path = Path(args.docs)
    hashes = [x.strip() for x in docs_path.read_text(encoding="utf-8").splitlines()
              if x.strip() and not x.startswith("#")]
    if not hashes:
        print(f"[fatal] 清单为空：{docs_path}", file=sys.stderr)
        return 2
    result = run(hashes, Path(args.db), Path(args.corpus_root))

    if args.make:
        Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")
        print(json.dumps({"baseline": str(args.baseline), "total": result["total"]},
                         ensure_ascii=False))
        return 0

    base_path = Path(args.baseline)
    if not base_path.exists():
        print(f"[fatal] 基线不存在：{base_path}（先跑 --make）", file=sys.stderr)
        return 2
    base = json.loads(base_path.read_text(encoding="utf-8"))
    verdict = compare(result, base)
    if args.json:
        print(json.dumps({"current": result["total"], "baseline": base.get("total"),
                          **verdict}, ensure_ascii=False, indent=1))
    else:
        print(f"[{verdict['verdict'].lower()}] 题 {result['total']['questions']} "
              f"(基线 {base.get('total', {}).get('questions')})，"
              f"空答案 {result['total']['empty_answers']}，"
              f"弱题 {result['total']['weak_questions']}，缺 {result['missing']} 篇")
        for f in verdict["fails"]:
            print("  - " + f)
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
