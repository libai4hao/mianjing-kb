# -*- coding: utf-8 -*-
"""P1-4 report：同题频次报告——questions 表 → 近重复簇 → Markdown + CSV。

- 精确同文与近重复统一走 question_freq.cluster（simhash 16bit 分带 LSH 候选
  + 汉明≤dist + 2-gram Jaccard≥jac 双确认）；同文必然同簇（simhash 相同同桶）。
- 输出：build/question_freq_report.md（Top 表 + 改写对样例 + 总览）
       build/question_freq_clusters.csv（全部 ≥2 文档的簇，含 doc_hash 清单）

用法：
    python -m mianjing_kb.report                 # 默认阈值 dist=24 jac=0.7
    python -m mianjing_kb.report --top 100
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db  # noqa: E402
from mianjing_kb.question_freq import cluster  # noqa: E402


def _unsigned64(v: int) -> int:
    """库里 simhash 是二补码有符号，聚类要还原成无符号。"""
    return v + (1 << 64) if v < 0 else v


def load_questions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT q.doc_hash, q.seq, q.q_text, q.q_norm, q.q_norm_hash, q.simhash, q.qtype, "
        "       q.q_key, sd.file_name, sd.topic, sd.collection "
        "FROM questions q LEFT JOIN source_documents sd ON sd.doc_hash = q.doc_hash"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["simhash_u"] = _unsigned64(r["simhash"])
        out.append(d)
    return out


def build_clusters(qs: list[dict], max_dist: int, min_jac: float):
    """返回簇列表：每簇 {members, docs, sample, variants, qtype}，按文档数降序。"""
    items = [(q["q_norm"], q["simhash_u"]) for q in qs]
    dsu = cluster(items, max_dist, min_jac)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(qs)):
        groups[dsu.find(i)].append(i)
    clusters = []
    for members in groups.values():
        docs = {qs[i]["doc_hash"] for i in members}
        # 见证数口径（Q-2）：distinct collection；空 collection 回退自身 doc_hash
        witnesses = {qs[i].get("collection") or "doc:" + qs[i]["doc_hash"] for i in members}
        norms = {qs[i]["q_norm"] for i in members}
        # 样例题：取最常见 norm 里最长的原文
        by_norm: dict[str, list[int]] = defaultdict(list)
        for i in members:
            by_norm[qs[i]["q_norm"]].append(i)
        best_norm = max(by_norm, key=lambda n: (len(by_norm[n]), len(n)))
        qtypes = [qs[i]["qtype"] for i in members]
        clusters.append({
            "members": members,
            "n_docs": len(docs),
            "n_collections": len(witnesses),
            "witness": len(witnesses),
            "doc_hashes": sorted(docs),
            "sample": qs[by_norm[best_norm][0]]["q_text"],
            "n_variants": len(norms),
            "variants": sorted(norms),
            "qtype": max(set(qtypes), key=qtypes.count),
        })
    clusters.sort(key=lambda c: (-c["n_collections"], -c["n_docs"], -len(c["members"])))
    return clusters


def write_report(path: Path, qs: list[dict], clusters: list, top: int, dist: int, jac: float,
                 elapsed: float) -> dict:
    multi = [c for c in clusters if c["n_collections"] >= 2]
    n_in_multi = sum(len(c["members"]) for c in multi)
    qtype_dist = defaultdict(int)
    for q in qs:
        qtype_dist[q["qtype"]] += 1
    lines = [
        "# 同题频次报告（P1-4）",
        "",
        f"- 口径：questions 表 {len(qs)} 题 → simhash LSH（dist≤{dist}, jaccard≥{jac}）聚类",
        "- 见证数 = distinct collection（同一来源集只计 1；collection 为空回退 doc_hash，Q-2 2026-11-19）",
        f"- 簇总数 {len(clusters)}；**≥2 来源集的簇 {len(multi)} 个**，覆盖 {n_in_multi} 题"
        f"（{n_in_multi / max(len(qs), 1) * 100:.1f}%）",
        f"- qtype 分布：{json.dumps(dict(sorted(qtype_dist.items(), key=lambda x: -x[1])), ensure_ascii=False)}",
        "",
        f"## 频次 Top {min(top, len(multi))}",
        "",
        "| # | 题目样例 | 见证(collection) | 文档数 | 变体数 | qtype |",
        "|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(multi[:top], 1):
        lines.append(f"| {i} | {c['sample'][:60]} | {c['witness']} | {c['n_docs']} | "
                     f"{c['n_variants']} | {c['qtype']} |")
    # 改写对样例：同簇不同 norm 的前 20 簇各取一对
    lines += ["", "## 近重复改写对样例（同簇不同措辞）", ""]
    shown = 0
    for c in multi:
        if c["n_variants"] < 2 or shown >= 20:
            continue
        a, b = c["variants"][0], c["variants"][1]
        lines.append(f"- 「{a[:40]}」 ↔ 「{b[:40]}」（{c['n_docs']} 文档）")
        shown += 1
    if not shown:
        lines.append("- 无")
    lines += ["", f"*生成耗时 {elapsed:.1f}s*"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"questions": len(qs), "clusters": len(clusters), "multi_doc_clusters": len(multi),
            "questions_in_multi": n_in_multi,
            "pct_in_multi": round(n_in_multi / max(len(qs), 1) * 100, 1)}


def write_csv(path: Path, clusters: list, top: int) -> int:
    multi = [c for c in clusters if c["n_collections"] >= 2]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "sample_q", "witness", "n_docs", "n_variants", "qtype", "doc_hashes"])
        for i, c in enumerate(multi[:top], 1):
            w.writerow([i, c["sample"], c["witness"], c["n_docs"], c["n_variants"], c["qtype"],
                        " ".join(c["doc_hashes"])])
    return len(multi[:top])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P1-4：同题频次报告")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--dist", type=int, default=24)
    ap.add_argument("--jac", type=float, default=0.7)
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--out-md", default=str(config.BUILD_DIR / "question_freq_report.md"))
    ap.add_argument("--out-csv", default=str(config.BUILD_DIR / "question_freq_clusters.csv"))
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=False)
    qs = load_questions(conn)
    conn.close()
    started = time.perf_counter()
    clusters = build_clusters(qs, args.dist, args.jac)
    elapsed = time.perf_counter() - started
    summary = write_report(Path(args.out_md), qs, clusters, args.top, args.dist, args.jac, elapsed)
    n_csv = write_csv(Path(args.out_csv), clusters, args.top)
    summary["csv_rows"] = n_csv
    summary["elapsed_sec"] = round(elapsed, 1)
    summary["out_md"] = args.out_md
    summary["out_csv"] = args.out_csv
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
