# -*- coding: utf-8 -*-
"""cluster：题级吸附（P0-1）+ 99 区可删 gate。

方案 v2-R2 阈值：
  - 归一化后 2-gram Jaccard >= 0.92（或精确同文）→ 强并（auto）
  - 0.85 <= Jaccard < 0.92 → 待人工（pending，不并簇，只记录）
  - SimHash(64bit) 16bit 分带 LSH 出候选，Jaccard 复核（复用 question_freq 原语）

99 区判定（每件三态）：
  strong  — 该件任一题与库内题强并同簇 / 精确同文 → 可删
  manual  — 仅存在 0.85~0.92 的待人工对 → 等人工
  none    — 无任何匹配 → 保留（可能是书/讲义等非题库件，题目层对不上不代表内容无副本）

产物（build/）：
  cluster_result.json       统计 + 簇明细（>=2 成员）+ 99 区逐件判定
  cluster_pending.csv       待人工对（0.85~0.92）
  cluster_sample50.md       强簇抽样 50 个，供人工核准确率

用法：
    python tools/cluster.py                 # 全量
    python tools/cluster.py --limit 20      # 只抽前 20 件（调试）
    python tools/cluster.py --no-cache      # 忽略缓存重抽

缓存：build/cluster_text_cache.json，键 = 路径 + size + mtime；改文件自动失效。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb import config  # noqa: E402
from question_freq import (DSU, extract, grams, hamming, jaccard, normalize,  # noqa: E402
                           simhash, split_questions)

DOC_EXTS = {".pdf", ".docx", ".doc", ".md", ".txt"}

POOL_TOPS = ["01-Java基础", "02-Java进阶", "03-数据库", "04-前端", "05-大数据",
             "06-云计算与运维", "07-中间件与分布式", "08-计算机网络与操作系统",
             "09-算法与数据结构", "10-软技能与职业", "11-面经与真题", "12-综合题库",
             "13-操作系统", "14-数据库专题", "15-网络专题", "16-JVM专题",
             "17-并发编程", "18-Spring生态", "19-其他专题", "31-收件箱"]
DIR99 = "99-重复文件待删"
DIR20 = "20-题库MD"


def iter_docs(root: Path, md_only: bool = False):
    if not root.exists():
        return
    for dirpath, _dn, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in DOC_EXTS and not (md_only and p.suffix.lower() != ".md"):
                yield p


class TextCache:
    """切题结果缓存：键 (path, size, mtime)，值 = 题目块列表。"""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.data: dict = {}
        if enabled and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, p: Path):
        try:
            st = p.stat()
        except OSError:
            return None, False
        key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        return self.data.get(key), key

    def put(self, key: str, questions: list):
        self.data[key] = questions

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def collect_questions(paths, cache: TextCache, stats: defaultdict, limit: int = 0):
    """返回 [(question_norm, question_raw, doc_rel)]；抽取失败/无文本记账不中断。"""
    out = []
    n = 0
    for p in paths:
        if limit and n >= limit:
            break
        rel = str(p.relative_to(config.CORPUS_ROOT)) if config.CORPUS_ROOT in p.parents else str(p)
        cached, key = cache.get(p)
        if cached is not None:
            qs = cached
            stats["cache_hit"] += 1
        else:
            try:
                if p.suffix.lower() == ".md" or p.suffix.lower() == ".txt":
                    text = p.read_text(encoding="utf-8", errors="replace")
                else:
                    text = extract(p)
                if len(text.strip()) < 80:
                    text = ""
                else:
                    # split_questions 只认行首题号，md 标题井号先剥掉（长度判据用剥前原文）
                    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', text)
            except Exception as ex:  # noqa: BLE001
                stats["error"] += 1
                print(f"[warn] 抽取失败 {rel}: {type(ex).__name__}", file=sys.stderr)
                cache.put(key, [])
                continue
            qs = split_questions(text) if text else []
            cache.put(key, qs)
        stats["docs"] += 1
        stats["questions"] += len(qs)
        for q in qs:
            nq = normalize(q)
            if len(nq) < 6:
                stats["too_short"] += 1
                continue
            out.append((nq, q, rel))
        n += 1
    return out


def build_clusters(items, auto: float, manual: float):
    """items = [(norm, simhash, rel)] → (dsu, pending_pairs, strong_pairs)。

    强并条件：精确同文 或 (LSH 候选 & 汉明<=24 & Jaccard>=auto)。
    0.85<=J<0.92 只记 pending，不 union。
    """
    hashes = [simhash(t) for t, _, _ in items]
    gsets = [grams(t) for t, _, _ in items]
    dsu = DSU(len(items))
    buckets = defaultdict(list)
    for idx, h in enumerate(hashes):
        for band in range(8):                      # 8×8bit 分带：比 4×16 召回高（差 6 位的近重复对也能撞带）
            buckets[(band, (h >> (band * 8)) & 0xFF)].append(idx)
    pending, strong = [], []
    seen_pair = set()
    for cands in buckets.values():
        if len(cands) < 2:
            continue
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                a, b = cands[i], cands[j]
                pk = (min(a, b), max(a, b))
                if pk in seen_pair or dsu.find(a) == dsu.find(b):
                    continue
                seen_pair.add(pk)
                if hamming(hashes[a], hashes[b]) > 24:
                    continue
                jac = jaccard(gsets[a], gsets[b])
                if jac >= auto:
                    dsu.union(a, b)
                    strong.append((a, b, round(jac, 4)))
                elif jac >= manual:
                    pending.append((a, b, round(jac, 4)))
    # 精确同文强并（不同措辞归一后撞车）
    exact = defaultdict(list)
    for i, (t, _, _) in enumerate(items):
        exact[t].append(i)
    for idxs in exact.values():
        for k in idxs[1:]:
            dsu.union(idxs[0], k)
            strong.append((idxs[0], k, 1.0))
    return dsu, pending, strong


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题级吸附 + 99 区 gate")
    ap.add_argument("--auto", type=float, default=0.92, help="强并 Jaccard 阈值")
    ap.add_argument("--manual", type=float, default=0.85, help="待人工下限")
    ap.add_argument("--sample", type=int, default=50, help="强簇抽样数（人工核）")
    ap.add_argument("--limit", type=int, default=0, help="每类最多处理 N 件（调试）")
    ap.add_argument("--no-cache", action="store_true", help="忽略抽取缓存")
    ap.add_argument("--out", default=str(config.BUILD_DIR / "cluster_result.json"))
    args = ap.parse_args(argv)

    R = config.CORPUS_ROOT
    t0 = time.perf_counter()
    stats: defaultdict = defaultdict(int)

    pool_paths = [p for d in POOL_TOPS for p in iter_docs(R / d)]
    pool_paths += list(iter_docs(R / DIR20, md_only=True))
    docs99 = sorted(iter_docs(R / DIR99))

    cache = TextCache(config.BUILD_DIR / "cluster_text_cache.json", not args.no_cache)
    print(f"[pool] 库内 {len(pool_paths)} 件，99 区 {len(docs99)} 件", flush=True)
    pool_q = collect_questions(pool_paths, cache, stats, args.limit)
    q99 = collect_questions(docs99, cache, stats, args.limit)
    cache.save()

    items = [(t, h, rel) for (t, q, rel), h in
             [((t, q, rel), simhash(t)) for (t, q, rel) in pool_q + q99]]
    n_pool = len(pool_q)
    print(f"[items] 库内题 {n_pool}，99 区题 {len(q99)}，共 {len(items)}", flush=True)

    dsu, pending, strong = build_clusters(items, args.auto, args.manual)

    groups = defaultdict(list)
    for i in range(len(items)):
        groups[dsu.find(i)].append(i)

    # 簇明细：只留 >=2 成员且含库内题的
    clusters_out = []
    for root, idxs in groups.items():
        if len(idxs) < 2:
            continue
        members = [{"rel": items[i][2], "q": items[i][0][:120],
                    "from99": i >= n_pool} for i in idxs]
        if all(m["from99"] for m in members):
            continue  # 99 区内部互抱，没有库内见证，不可删
        clusters_out.append({
            "size": len(idxs),
            "n99": sum(1 for m in members if m["from99"]),
            "members": members,
        })
    clusters_out.sort(key=lambda c: (-c["size"], -c["n99"]))

    # 99 区逐件判定：按"件"聚合取最优档（strong > manual > none）；无题件默认 none
    root_has_pool = {dsu.find(i) for i in range(n_pool)}
    pending_pool_roots = set()
    pending_by_99item: dict[str, list] = defaultdict(list)
    for a, b, jac in pending:
        ia, ib = (a, b) if a < n_pool else (b, a)
        if ia < n_pool and ib >= n_pool:  # 跨库内/99 区的待人工对
            rel99 = items[ib][2]
            pending_by_99item[rel99].append({"pool_rel": items[ia][2],
                                             "pool_rel_q": items[ia][0][:120],
                                             "q99": items[ib][0][:120], "jac": jac})
            pending_pool_roots.add(dsu.find(ia))

    agg: dict[str, dict] = defaultdict(lambda: {"strong": 0, "manual": 0, "none": 0, "best_size": 0})
    for i in range(n_pool, len(items)):
        rel = items[i][2]
        r = dsu.find(i)
        if r in root_has_pool:
            v = "strong"
        elif r in pending_pool_roots or rel in pending_by_99item:
            v = "manual"
        else:
            v = "none"
        a = agg[rel]
        a[v] += 1
        if v == "strong":
            a["best_size"] = max(a["best_size"], len(groups[r]))
    rels_all = []
    for p in docs99:
        rels_all.append(str(p.relative_to(config.CORPUS_ROOT)) if config.CORPUS_ROOT in p.parents else str(p))
    verdicts = []
    for rel in rels_all:
        a = agg.get(rel)
        if not a:
            verdicts.append({"rel": rel, "verdict": "none", "cluster_size": 0, "pending": []})
            continue
        v = "strong" if a["strong"] else ("manual" if a["manual"] else "none")
        verdicts.append({"rel": rel, "verdict": v, "cluster_size": a["best_size"],
                         "pending": pending_by_99item.get(rel, [])})
    cnt = defaultdict(int)
    for v in verdicts:
        cnt[v["verdict"]] += 1

    # 强簇抽样（人工核准确率）
    sample = random.Random(42).sample(clusters_out, min(args.sample, len(clusters_out))) \
        if clusters_out else []

    result = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "thresholds": {"auto": args.auto, "manual": args.manual},
        "stats": {**stats, "pool_questions": n_pool, "q99_questions": len(q99),
                  "clusters_ge2_with_pool": len(clusters_out),
                  "strong_pairs": len(strong), "pending_pairs": len(pending)},
        "verdict99": dict(cnt),
        "verdict_detail": verdicts,
        "clusters_top": clusters_out[:300],
        "sample": sample,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    pend_path = config.BUILD_DIR / "cluster_pending.csv"
    with pend_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rel99", "q99", "pool_rel", "pool_q", "jaccard"])
        for rel99, pairs in pending_by_99item.items():
            for pp in pairs:
                w.writerow([rel99, pp["q99"], pp["pool_rel"], pp["pool_rel_q"], pp["jac"]])

    sample_path = config.BUILD_DIR / "cluster_sample50.md"
    lines = ["# cluster 强簇抽样（人工核准确率用）", "",
             f"- 阈值 auto={args.auto} / manual={args.manual}；抽样 {len(sample)}/{len(clusters_out)} 簇",
             "- 核法：看同簇成员的题是否真是同一题；误并请记簇内两行题文", ""]
    for k, c in enumerate(sample, 1):
        lines.append(f"## 簇 {k}（{c['size']} 成员，99 区 {c['n99']}）")
        for m in c["members"][:8]:
            tag = "【99】" if m["from99"] else "【库】"
            lines.append(f"- {tag} `{m['rel'].split(chr(92))[-1]}`：{m['q']}")
        if len(c["members"]) > 8:
            lines.append(f"- …共 {c['size']} 成员")
        lines.append("")
    sample_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({
        "docs": stats["docs"], "questions": stats["questions"],
        "clusters_ge2_with_pool": len(clusters_out),
        "strong_pairs": len(strong), "pending_pairs": len(pending),
        "verdict99": dict(cnt),
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }, ensure_ascii=False, indent=1))
    print(f"[out] {out}\n[out] {pend_path}\n[out] {sample_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
