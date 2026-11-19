# -*- coding: utf-8 -*-
"""题簇成对评测 + 语料质量指标（P1：让聚类质量可被数字证明）。

两类输出，互相独立、都不改库：
  ① 成对判定：`eval/cluster_pairs.yaml` 里人工标注的同题/不同题对，按生产聚类同款判据
     （归一化 → SimHash 汉明距离 ≤ dist → 2-gram Jaccard ≥ jac）逐对判定，
     算 Precision / Recall / F1，并按 `kind` 分组（normalize / paraphrase / hard_neg / long_noise）。
  ② 语料指标：从 `kb.sqlite` 取题长与答案覆盖分布；从 `20-题库MD` 重算 chunk_v2 的
     coverage 与 leftovers 比例（不改数据、不落库）。

口径提醒（写进报告，也写进这里）：
  - 成对 P/R 是**成对判定**的指标，不等价于"簇级 P/R"：传递闭包可能把 A-B、B-C 的
    pair 合并成 A-B-C 的误簇。要严格簇级指标需人工标注整簇，当前先以成对指标为代理。
  - `eval/cluster_pairs.yaml` 的 `review_state=draft` 时只作趋势参考，不作验收阈值。
  - coverage 用 micro-average（Σ净覆盖 / Σ可计入长度），与单篇 coverage 的中位数分开报。

用法：
    python -m mianjing_kb.eval_cluster --sweep
    python -m mianjing_kb.eval_cluster --dist 24 --jac 0.7 --db <kb.sqlite> --md-root <20-题库MD>
    python -m mianjing_kb.eval_cluster --skip-corpus --min-f1 0.5
退出码：0 正常；1 F1 低于 --min-f1；2 输入缺失。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config  # noqa: E402
from mianjing_kb.question_freq import grams, hamming, jaccard, normalize, simhash  # noqa: E402
from mianjing_kb.chunker import chunk, chunk_v2  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
KINDS = ("normalize", "paraphrase", "hard_neg", "long_noise")
DEFAULT_DISTS = (16, 20, 24, 28, 32)
DEFAULT_JACS = (0.4, 0.5, 0.6, 0.7, 0.8)
CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------- 成对评测 ----------------
def load_pairs(path: Path) -> tuple[dict, list[dict]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    meta = data.get("meta") or {}
    rows = data.get("pairs") or []
    if not rows:
        raise ValueError(f"{path} 里没有 pairs")
    seen = set()
    for i, r in enumerate(rows, 1):
        pid = str(r.get("id") or f"#{i}")
        if pid in seen:
            raise ValueError(f"{path} 的 id 重复：{pid}")
        seen.add(pid)
        if not isinstance(r.get("same"), bool):
            raise ValueError(f"{pid}: same 必须是 true/false")
        if not str(r.get("a") or "").strip() or not str(r.get("b") or "").strip():
            raise ValueError(f"{pid}: a/b 不能为空")
        if r.get("kind") not in KINDS:
            raise ValueError(f"{pid}: kind 必须是 {KINDS}")
    return meta, rows


def pair_features(a: str, b: str) -> dict:
    na, nb = normalize(a), normalize(b)
    ha, hb = simhash(na), simhash(nb)
    return {"hamming": hamming(ha, hb),
            "jaccard": round(jaccard(grams(na), grams(nb)), 4),
            "norm_equal": na == nb,
            "len_a": len(a), "len_b": len(b)}


def predict(feat: dict, dist: int, jac: float) -> bool:
    """生产聚类同款判据（见 question_freq.cluster）。"""
    return feat["hamming"] <= dist and feat["jaccard"] >= jac


def _ratio(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def score(rows: list[dict], dist: int, jac: float, kind: str | None = None) -> dict:
    """rows: [{same, feat, id, a, b}]；返回含 tp/fp/fn/tn 与 P/R/F1 的 dict。"""
    picked = [r for r in rows if kind is None or r["kind"] == kind]
    tp = fp = fn = tn = 0
    fails = []
    for r in picked:
        pred = predict(r["feat"], dist, jac)
        if pred and r["same"]:
            tp += 1
        elif pred and not r["same"]:
            fp += 1
            fails.append({"id": r["id"], "type": "误合并", "a": r["a"][:60], "b": r["b"][:60],
                          "hamming": r["feat"]["hamming"], "jaccard": r["feat"]["jaccard"]})
        elif not pred and r["same"]:
            fn += 1
            fails.append({"id": r["id"], "type": "漏合并", "a": r["a"][:60], "b": r["b"][:60],
                          "hamming": r["feat"]["hamming"], "jaccard": r["feat"]["jaccard"]})
        else:
            tn += 1
    return {"n": len(picked), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
            "f1": round(2 * tp / (2 * tp + fp + fn), 4) if tp else 0.0,
            "fails": fails}


def sweep(rows: list[dict], dists=DEFAULT_DISTS, jacs=DEFAULT_JACS) -> list[dict]:
    out = []
    for d in dists:
        for j in jacs:
            s = score(rows, d, j)
            out.append({"dist": d, "jac": j, "precision": s["precision"],
                        "recall": s["recall"], "f1": s["f1"], "fp": s["fp"], "fn": s["fn"]})
    return out


def _pct(v: list[float], q: float) -> float:
    """最近秩百分位（不插值），空表返回 0。"""
    if not v:
        return 0.0
    s = sorted(v)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


def _tokens(text: str) -> int:
    cjk = len(CJK.findall(text))
    return cjk + (len(text) - cjk) // 4


# ---------------- 语料指标 ----------------
def db_stats(db_path: Path) -> dict:
    from mianjing_kb.kb import db

    conn = db.connect(db_path, create=False)
    rows = conn.execute("SELECT q_text, a_text FROM questions").fetchall()
    conn.close()
    qlens = [len(r[0] or "") for r in rows]
    atext = [r[1] or "" for r in rows]
    answered = [t for t in atext if len(t.strip()) >= 20]
    out = {
        "questions": len(rows),
        "q_chars": {"p50": _pct(qlens, 0.50), "p90": _pct(qlens, 0.90),
                    "p99": _pct(qlens, 0.99), "max": max(qlens) if qlens else 0},
        "q_tokens_p50": _pct([_tokens(r[0] or "") for r in rows], 0.50),
        "q_over_120_tokens": sum(1 for r in rows if _tokens(r[0] or "") > 120),
        "answer_coverage": _ratio(len(answered), len(rows)),
        "answer_chars_p50": _pct([len(t) for t in atext], 0.50),
        "empty_answer": sum(1 for t in atext if not t.strip()),
    }
    if rows:
        out["q_over_120_tokens_pct"] = _ratio(out["q_over_120_tokens"], len(rows))
        out["empty_answer_pct"] = _ratio(out["empty_answer"], len(rows))
    return out


def md_stats(md_root: Path, limit: int = 0) -> dict:
    files = sorted(p for p in md_root.rglob("*.md"))
    if limit:
        files = files[:limit]
    total = docs = zero = 0
    covered = denom = 0
    covs: list[float] = []
    left_chars = 0
    left_by: dict[str, int] = {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text:
            continue
        docs += 1
        total += len(text)
        v2 = chunk_v2(text)
        v1 = chunk(text)
        covs.append(v2["coverage"])
        item_len = _merged_len([b["source_span"] for b in v1["blocks"]])
        noise = _overlap_len([b["source_span"] for b in v1["blocks"]],
                             [l["span"] for l in v1["leftovers"] if l["why"] != "未识别正文"])
        deduct = _merged_len([l["span"] for l in v1["leftovers"] if l["why"] != "未识别正文"])
        covered += item_len - noise
        denom += max(1, len(text) - deduct)
        if not v2["items"]:
            zero += 1
        for l in v1["leftovers"]:
            if l["why"] == "未识别正文":
                n = l["span"][1] - l["span"][0]
                left_chars += n
                left_by["未识别正文"] = left_by.get("未识别正文", 0) + n
    return {
        "md_docs": docs, "chars": total, "docs_zero_items": zero,
        "coverage_micro": _ratio(covered, denom),
        "coverage_p10": _pct(covs, 0.10), "coverage_p50": _pct(covs, 0.50),
        "coverage_p90": _pct(covs, 0.90),
        "leftover_unrecognized_chars": left_chars,
        "leftover_unrecognized_ratio": _ratio(left_chars, total),
        "leftover_by_why": left_by,
    }


def _merged_len(spans) -> int:
    total, cur = 0, None
    for s, e in sorted((int(a), int(b)) for a, b in spans if b > a):
        if cur and s <= cur[1]:
            cur = (cur[0], max(cur[1], e))
        else:
            if cur:
                total += cur[1] - cur[0]
            cur = (s, e)
    if cur:
        total += cur[1] - cur[0]
    return total


def _overlap_len(a, b) -> int:
    aa = _merged_intervals(a)
    bb = _merged_intervals(b)
    i = j = total = 0
    while i < len(aa) and j < len(bb):
        lo, hi = max(aa[i][0], bb[j][0]), min(aa[i][1], bb[j][1])
        if lo < hi:
            total += hi - lo
        if aa[i][1] < bb[j][1]:
            i += 1
        else:
            j += 1
    return total


def _merged_intervals(spans):
    out = []
    for s, e in sorted((int(x), int(y)) for x, y in spans if y > x):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# ---------------- 报告 ----------------
def _has_placeholder(path: Path) -> bool:
    """配置未注入时 BUILD_DIR 会保留 `<CORPUS_ROOT>` 占位段，Windows 下 mkdir 会报 WinError 123。"""
    return any("<" in part or ">" in part for part in Path(path).parts)


def write_report(path: Path, meta: dict, base: dict, by_kind: dict, sweep_rows: list[dict],
                 dbs: dict | None, mds: dict | None) -> None:
    lines = ["# 题簇评测报告（pair-level）", "",
             f"- 评测集：`{meta.get('name', '')}` v{meta.get('version', '?')}，"
             f"{base['n']} 对（same {base['tp'] + base['fn']} / diff {base['tn'] + base['fp']}）",
             f"- 标注状态：**{meta.get('review_state', '未知')}**"
             + ("（人工复核前只作趋势参考，不作验收阈值）" if meta.get("review_state") == "draft" else ""),
             f"- 判据：SimHash 汉明 ≤ {base['dist']} 且 2-gram Jaccard ≥ {base['jac']}"
             f"（与 `question_freq.cluster` 同款）",
             "",
             "## 1. 总体指标", "",
             "| 指标 | 值 |", "|---|---|",
             f"| Precision | **{base['precision']:.4f}**（TP {base['tp']} / FP {base['fp']}） |",
             f"| Recall | **{base['recall']:.4f}**（TP {base['tp']} / FN {base['fn']}） |",
             f"| F1 | **{base['f1']:.4f}** |",
             f"| TN | {base['tn']} |", "",
             "## 2. 分组指标（同一阈值）", "",
             "| kind | n | P | R | F1 | FP | FN |", "|---|---|---|---|---|---|---|"]
    for k in KINDS:
        s = by_kind.get(k)
        if not s:
            continue
        lines.append(f"| {k} | {s['n']} | {s['precision']:.4f} | {s['recall']:.4f} | "
                     f"{s['f1']:.4f} | {s['fp']} | {s['fn']} |")
    if base["fails"]:
        lines += ["", "## 3. 误判明细（当前阈值）", "",
                  "| id | 类型 | hamming | jaccard | a | b |", "|---|---|---|---|---|---|"]
        for f in base["fails"]:
            lines.append(f"| {f['id']} | {f['type']} | {f['hamming']} | {f['jaccard']} | "
                         f"{f['a'].replace('|', '/')} | {f['b'].replace('|', '/')} |")
    if sweep_rows:
        lines += ["", "## 4. 阈值扫描（找 operating point，不自动改代码）", "",
                  "| dist | jac | P | R | F1 | FP | FN |", "|---|---|---|---|---|---|---|"]
        for r in sweep_rows:
            mark = " ←默认" if r["dist"] == 24 and r["jac"] == 0.7 else ""
            lines.append(f"| {r['dist']} | {r['jac']} | {r['precision']:.4f} | {r['recall']:.4f} | "
                         f"{r['f1']:.4f} | {r['fp']} | {r['fn']} |{mark}")
    if dbs:
        lines += ["", "## 5. 题长与答案覆盖（kb.sqlite）", "",
                  "| 指标 | 值 |", "|---|---|",
                  f"| 题行 | {dbs['questions']} |",
                  f"| 题干字符 p50 / p90 / p99 / max | {dbs['q_chars']['p50']} / {dbs['q_chars']['p90']} / "
                  f"{dbs['q_chars']['p99']} / {dbs['q_chars']['max']} |",
                  f"| 题干 >120 token | {dbs['q_over_120_tokens']}"
                  f"（{dbs.get('q_over_120_tokens_pct', 0):.2%}） |",
                  f"| 答案覆盖率（a_text ≥20 字） | {dbs['answer_coverage']:.4f} |",
                  f"| 空答案 | {dbs['empty_answer']}（{dbs.get('empty_answer_pct', 0):.2%}） |"]
    if mds:
        lines += ["", "## 6. 切题覆盖与 leftovers（20-题库MD 重算）", "",
                  "| 指标 | 值 |", "|---|---|",
                  f"| md 文档 | {mds['md_docs']}（0 题文档 {mds['docs_zero_items']}） |",
                  f"| coverage（micro / p10 / p50 / p90） | {mds['coverage_micro']:.4f} / "
                  f"{mds['coverage_p10']:.4f} / {mds['coverage_p50']:.4f} / {mds['coverage_p90']:.4f} |",
                  f"| 未识别正文占比 | {mds['leftover_unrecognized_ratio']:.4f}"
                  f"（{mds['leftover_unrecognized_chars']} 字符） |"]
    lines += ["", "## 7. 口径与局限", "",
              "- 这是**成对判定**指标，不是簇级 P/R：传递合并可能把多条边并成误簇，需整簇标注才能直接测。",
              "- `draft` 评测集由代码作者起草，未经逐条复核；复核前不要把阈值调参结果写进生产默认值。",
              "- coverage 是 micro-average（Σ净覆盖 / Σ可计入长度）；单篇 coverage 分布另给 p10/p50/p90。",
              "- leftovers 只统计 `why=未识别正文`（目录/页码/页眉等已被扣除，不计入缺口）。"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题簇成对评测 + 语料质量指标")
    ap.add_argument("--pairs", default=str(REPO / "eval" / "cluster_pairs.yaml"))
    ap.add_argument("--dist", type=int, default=24)
    ap.add_argument("--jac", type=float, default=0.7)
    ap.add_argument("--sweep", action="store_true", help="输出阈值扫描表")
    ap.add_argument("--db", default=str(config.DB_PATH), help="kb.sqlite；--skip-corpus 时忽略")
    ap.add_argument("--md-root", default=str(config.QUESTION_MD_DIR))
    ap.add_argument("--skip-corpus", action="store_true", help="只跑成对评测")
    ap.add_argument("--limit-md", type=int, default=0)
    ap.add_argument("--out-md", default="")
    ap.add_argument("--min-f1", type=float, default=0.0, help="低于此值 exit 1")
    args = ap.parse_args(argv)

    if not args.out_md and _has_placeholder(config.BUILD_DIR):
        print(json.dumps({
            "error": "输出目录未配置：config.BUILD_DIR 仍是占位值；"
                     "请传 --out-md，或设置 KB_CORPUS_ROOT / KB_BUILD_DIR",
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    pairs_path = Path(args.pairs)
    if not pairs_path.exists():
        print(json.dumps({"error": f"评测集不存在：{pairs_path}"}, ensure_ascii=False))
        return 2
    try:
        meta, rows = load_pairs(pairs_path)
    except ValueError as ex:
        print(json.dumps({"error": str(ex)}, ensure_ascii=False))
        return 2
    rows = [dict(r, feat=pair_features(r["a"], r["b"])) for r in rows]

    base = score(rows, args.dist, args.jac)
    base.update({"dist": args.dist, "jac": args.jac})
    by_kind = {k: score(rows, args.dist, args.jac, k) for k in KINDS}
    by_kind = {k: v for k, v in by_kind.items() if v["n"]}
    sweep_rows = sweep(rows) if args.sweep else []

    dbs = mds = None
    if not args.skip_corpus:
        db_path = Path(args.db)
        if db_path.exists():
            dbs = db_stats(db_path)
        md_root = Path(args.md_root)
        if md_root.exists():
            mds = md_stats(md_root, args.limit_md)

    out_md = Path(args.out_md) if args.out_md else Path(config.BUILD_DIR) / "cluster_eval_report.md"
    write_report(out_md, meta, base, by_kind, sweep_rows, dbs, mds)
    summary = {"pairs": base["n"], "precision": base["precision"], "recall": base["recall"],
               "f1": base["f1"], "tp": base["tp"], "fp": base["fp"],
               "fn": base["fn"], "tn": base["tn"], "dist": args.dist, "jac": args.jac,
               "by_kind": {k: {kk: v[kk] for kk in ("n", "precision", "recall", "f1", "fp", "fn")}
                           for k, v in by_kind.items()},
               "db": dbs, "md": mds, "out_md": str(out_md)}
    if sweep_rows:
        best = max(sweep_rows, key=lambda r: r["f1"])
        summary["best_f1_row"] = best
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if base["f1"] < args.min_f1:
        print(f"[fail] F1 {base['f1']:.4f} < min-f1 {args.min_f1}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
