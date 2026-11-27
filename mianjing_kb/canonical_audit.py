# -*- coding: utf-8 -*-
"""canonical 星形审计：核对自动并簇没有通过中间题传递误合。

`canonical` 的自动合并契约是 2-gram Jaccard >= `ATTACH_JAC`（默认 0.7），且每个成员
必须直接与代表题达标。高相似题对分属不同星形簇可能是中心竞争，不再单独判为漏合。

  1. 星形不变量：成员与代表题直连，低于阈值即 FAIL
  2. 合法低相似对：同簇非代表题之间可以低于阈值，单独计数
  3. 改写证据：同簇且 `q_norm` 不同的对数 + 样例（"换了说法仍合上"的实证）
  4. 候选带：jaccard 落在 [band, 阈值) 的题对数量（全量重建不登记人工候选，只作计数）
  5. 高相似跨簇：保留计数与样例，供人工判断是否存在值得合并的中心竞争

用法：
    python -m mianjing_kb.canonical_audit --db build/kb.sqlite
    python -m mianjing_kb.canonical_audit --db build/kb.sqlite --out build/canonical_audit.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import canonical as cn  # noqa: E402
from mianjing_kb import report as rp  # noqa: E402
from mianjing_kb.question_freq import short_latin_conflict  # noqa: E402

MAX_PAIRS_DEFAULT = 80_000_000


def _ro_uri(path: Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _within_stats(qs: list[dict], clusters: list[dict], grams: dict,
                  jac: float, samples: int, max_pairs: int = MAX_PAIRS_DEFAULT) -> dict:
    """同簇成对统计，并单独校验每个成员与代表题的直连关系。

    #72：簇内全对统计同样受 max_pairs 预算约束——它先于 _enumerate_pairs 运行，
    病态大簇（重复文本展开）不得在预算闸之前做 O(k²) 比较。
    """
    pairs = below = paraphrase = anchor_below = anchor_checks = 0
    rewrite_samples: list[dict] = []
    chain_samples: list[dict] = []
    star_violation_samples: list[dict] = []
    sampled_rewrite: set[int] = set()
    sampled_chain: set[int] = set()
    for ci, c in enumerate(clusters):
        ms = c["members"]
        anchor = c.get("representative_index")
        anchor_checks += max(0, len(ms) - 1)
        for x in range(len(ms)):
            for y in range(x + 1, len(ms)):
                if pairs >= max_pairs:
                    raise SystemExit(
                        f"簇内对数超过上限 {max_pairs}，调大 --max-pairs 或收窄输入")
                i, j = ms[x], ms[y]
                same = cn.jaccard(grams[i], grams[j])
                pairs += 1
                if same < jac:
                    below += 1
                    if anchor in (i, j):
                        anchor_below += 1
                        member = j if i == anchor else i
                        if len(star_violation_samples) < samples:
                            star_violation_samples.append({
                                "jac": round(same, 3),
                                "representative": qs[anchor]["q_text"][:80],
                                "member": qs[member]["q_text"][:80]})
                    elif len(chain_samples) < samples and ci not in sampled_chain:
                        sampled_chain.add(ci)
                        chain_samples.append({"jac": round(same, 3), "a": qs[i]["q_text"][:80],
                                              "b": qs[j]["q_text"][:80]})
                if qs[i]["q_norm"] != qs[j]["q_norm"]:
                    paraphrase += 1
                    # 每簇只取一条：否则一个 19 成员的 Spring 簇会占满 8 条样例。
                    if (len(rewrite_samples) < samples and same >= jac
                            and ci not in sampled_rewrite):
                        sampled_rewrite.add(ci)
                        rewrite_samples.append({"jac": round(same, 3), "a": qs[i]["q_text"][:80],
                                                "b": qs[j]["q_text"][:80]})
    return {"pairs": pairs, "below": below, "paraphrase": paraphrase,
            "anchor_checks": anchor_checks, "anchor_below": anchor_below,
            "rewrite_samples": rewrite_samples, "chain_samples": chain_samples,
            "star_violation_samples": star_violation_samples}


def _enumerate_pairs(qs: list[dict], grams: dict, owner: dict,
                     jac: float, band: float, max_pairs: int, samples: int) -> dict:
    """倒排前缀过滤后逐对精算 jaccard：统计契约对是否同簇、候选带对数。

    前缀过滤沿用 canonical 的 `_prefix_grams`（Jaccard>=t 的两集合必有共享 token
    落在双方前缀），所以它枚举的是"可能 >=t 的全部对"，不是抽样。
    """
    n = len(qs)
    index: dict[str, list[int]] = defaultdict(list)
    for i, g in grams.items():
        for token in cn._prefix_grams(g, jac):
            index[token].append(i)
    checked = compared = 0
    high = high_same = gated = band_pairs = 0
    violators: list[dict] = []
    gated_samples: list[dict] = []
    seen: set[int] = set()
    for lst in index.values():
        if len(lst) < 2:
            continue
        for x in range(len(lst)):
            i = lst[x]
            gi = grams[i]
            for y in range(x + 1, len(lst)):
                j = lst[y]
                if i == j:
                    continue
                compared += 1
                if compared > max_pairs:
                    raise SystemExit(f"候选对超过上限 {max_pairs}，调大 --max-pairs 或收窄 --jac")
                key = i * n + j if i < j else j * n + i
                if key in seen:
                    continue
                sim = cn.jaccard(gi, grams[j])
                if sim >= band:
                    # 只有跨过候选带的 pair 才值得去重：低于带的对重复精算很便宜，
                    # 而把它们全塞进 set 会让 2 万题的审计吃掉几百 MB。
                    seen.add(key)
                    if sim >= jac:
                        high += 1
                        checked += 1
                        if owner[i] == owner[j]:
                            high_same += 1
                        elif short_latin_conflict(qs[i]["q_norm"], qs[j]["q_norm"]):
                            # F6 闸门：短题拉丁实体不同，契约让位于“实体不同不并”
                            gated += 1
                            if len(gated_samples) < samples:
                                gated_samples.append({"jac": round(sim, 3),
                                                      "a": qs[i]["q_text"][:80],
                                                      "b": qs[j]["q_text"][:80]})
                        elif len(violators) < samples:
                            violators.append({"jac": round(sim, 3),
                                              "a": qs[i]["q_text"][:80],
                                              "b": qs[j]["q_text"][:80]})
                    else:
                        band_pairs += 1
    return {"compared": compared, "high": high, "high_same": high_same,
            "gated": gated, "band": band_pairs, "violators": violators,
            "gated_samples": gated_samples}


def audit_questions(qs: list[dict], jac: float = cn.ATTACH_JAC,
                    band: float = cn.CANDIDATE_JAC,
                    max_pairs: int = MAX_PAIRS_DEFAULT, samples: int = 8) -> dict:
    """对一批题目（report.load_questions 口径）做正向合并审计。"""
    clusters = rp.build_clusters(qs, 24, jac)
    owner: dict[int, int] = {}
    for cid, c in enumerate(clusters):
        for i in c["members"]:
            owner[i] = cid
    grams = {i: cn._grams(q["q_norm"]) for i, q in enumerate(qs)}
    inside = _within_stats(qs, clusters, grams, jac, samples, max_pairs)
    enum = _enumerate_pairs(qs, grams, owner, jac, band, max_pairs, samples)
    size_dist: dict[str, int] = defaultdict(int)
    for c in clusters:
        size_dist[str(len(c["members"]))] += 1
    return {
        "questions": len(qs),
        "clusters": len(clusters),
        "merged_away": len(qs) - len(clusters),
        "multi_doc_clusters": sum(1 for c in clusters if c["n_docs"] >= 2),
        "cluster_size_dist": dict(sorted(size_dist.items(), key=lambda kv: int(kv[0]))),
        "pairs_within_cluster": inside["pairs"],
        "pairs_within_below_jac": inside["below"],
        "pairs_within_paraphrase": inside["paraphrase"],
        "member_anchor_checks": inside["anchor_checks"],
        "pairs_member_below_anchor": inside["anchor_below"],
        "candidate_comparisons": enum["compared"],
        "pairs_jaccard_ge_jac": enum["high"],
        "pairs_ge_jac_same_cluster": enum["high_same"],
        "pairs_gated_by_short_latin": enum["gated"],
        "gated_samples": enum["gated_samples"],
        "pairs_ge_jac_cross_cluster": enum["high"] - enum["high_same"] - enum["gated"],
        "pairs_in_band": enum["band"],
        "rewrite_samples": inside["rewrite_samples"],
        "chain_samples": inside["chain_samples"],
        "star_violation_samples": inside["star_violation_samples"],
        "violators": enum["violators"],
    }


def audit_db(db_path: Path, jac: float = cn.ATTACH_JAC,
             band: float = cn.CANDIDATE_JAC,
             max_pairs: int = MAX_PAIRS_DEFAULT, samples: int = 8) -> dict:
    import sqlite3
    con = sqlite3.connect(_ro_uri(db_path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        qs = rp.load_questions(con)
    finally:
        con.close()
    out = audit_questions(qs, jac, band, max_pairs, samples)
    out["db"] = str(Path(db_path).resolve())
    return out


def render_md(data: dict) -> str:
    anchor_below = data.get("pairs_member_below_anchor", data["pairs_ge_jac_cross_cluster"])
    ok = anchor_below == 0
    lines = [
        "# canonical 星形挂靠审计", "",
        f"- 库：`{data.get('db', '')}`",
        f"- 题 {data['questions']} → 簇 {data['clusters']}（合并掉 {data['merged_away']}，"
        f"跨文档簇 {data['multi_doc_clusters']}）",
        f"- 星形不变量（成员直接相似于代表题）：**{anchor_below}** 个违规"
        f" → {'PASS' if ok else 'FAIL'}",
        f"- jaccard≥阈值 的题对同簇："
        f"**{data['pairs_ge_jac_same_cluster']}/{data['pairs_jaccard_ge_jac']}**"
        f"（跨簇 {data['pairs_ge_jac_cross_cluster']}；跨簇可能是星形中心竞争）",
        f"- 其中被 F6 短题拉丁实体闸门挡下的对：{data.get('pairs_gated_by_short_latin', 0)}"
        "（实体不同，属于契约的显式例外）",
        f"- 同簇非代表题对 jaccard 低于阈值（合法星形）：{data['pairs_within_below_jac']}"
        f"/{data['pairs_within_cluster']}",
        f"- 同簇且归一化文本不同（换说法仍合）：{data['pairs_within_paraphrase']}",
        f"- 候选带 [{data.get('band', cn.CANDIDATE_JAC)}, 阈值)：{data['pairs_in_band']} 对（全量只拆不合）",
        "",
    ]
    if data["rewrite_samples"]:
        lines += ["## 改写合并样例", ""]
        for s in data["rewrite_samples"]:
            lines.append(f"- jac={s['jac']}：`{s['a']}` ↔ `{s['b']}`")
        lines.append("")
    if data["chain_samples"]:
        lines += ["## 同簇非代表题低相似样例（合法星形）", ""]
        for s in data["chain_samples"]:
            lines.append(f"- jac={s['jac']}：`{s['a']}` ↔ `{s['b']}`")
        lines.append("")
    if data.get("star_violation_samples"):
        lines += ["## 星形违规样例（成员未直连代表题）", ""]
        for s in data["star_violation_samples"]:
            lines.append(f"- jac={s['jac']}：代表题 `{s['representative']}` ↔ 成员 `{s['member']}`")
        lines.append("")
    if data.get("gated_samples"):
        lines += ["## F6 闸门样例（jaccard 达标但实体不同，故意拆开）", ""]
        for s in data["gated_samples"]:
            lines.append(f"- jac={s['jac']}：`{s['a']}` ↔ `{s['b']}`")
        lines.append("")
    if data["violators"]:
        lines += ["## 高相似跨簇样例（星形竞争，不单独判失败）", ""]
        for s in data["violators"]:
            lines.append(f"- jac={s['jac']}：`{s['a']}` ↔ `{s['b']}`")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="canonical 正向合并审计（只读）")
    ap.add_argument("--db", required=True, help="kb.sqlite 路径")
    ap.add_argument("--jac", type=float, default=cn.ATTACH_JAC)
    ap.add_argument("--band", type=float, default=cn.CANDIDATE_JAC)
    ap.add_argument("--max-pairs", type=int, default=MAX_PAIRS_DEFAULT)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--out", default="", help="JSON 输出路径（可选）")
    ap.add_argument("--md", default="", help="Markdown 输出路径（可选）")
    args = ap.parse_args(argv)
    data = audit_db(Path(args.db), args.jac, args.band, args.max_pairs, args.samples)
    data["band"] = args.band
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.md:
        Path(args.md).write_text(render_md(data), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
