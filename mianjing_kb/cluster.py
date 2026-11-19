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
    python -m mianjing_kb.cluster                 # 全量
    python -m mianjing_kb.cluster --limit 20      # 只抽前 20 件（调试）
    python -m mianjing_kb.cluster --no-cache      # 忽略缓存重抽

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

from mianjing_kb.kb import config  # noqa: E402
from mianjing_kb.question_freq import (DSU, clean_question, close_word, extract, grams, hamming,  # noqa: E402
                           jaccard, normalize, simhash, split_qa)

ANSWER_AGREE = 0.45      # 答案块 Jaccard 低于此值 → 视为"同题不同答"，不许自动删

DOC_EXTS = {".pdf", ".docx", ".doc", ".md", ".txt"}
# chunker/切题口径变更必须让旧缓存整体失效；key 前缀版本号，避免 v1 的块被 v2 复用。
CACHE_VERSION = "v2-2026-11-19"

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
            if fn.startswith("~$"):
                continue          # Word 锁文件不是文档
            p = Path(dirpath) / fn
            if p.suffix.lower() in DOC_EXTS and not (md_only and p.suffix.lower() != ".md"):
                yield p


class TextCache:
    """切题结果缓存：键 (version, path, size, mtime)，值 = {questions, text_chars}。"""

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
        key = f"{CACHE_VERSION}|{p}|{st.st_size}|{int(st.st_mtime)}"
        return self.data.get(key), key

    def put(self, key: str, payload: dict):
        self.data[key] = payload

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)       # 原子替换，进程被杀不会留下半个缓存


def collect_questions(paths, cache: TextCache, stats: defaultdict, limit: int = 0):
    """返回 (items, doc_stats)。

    items = [(question_norm, question_raw, doc_rel, answer_norm)]；
    doc_stats[rel] = {text_chars, q_chars, n_q} 用于逐件文本覆盖率（不能只看全局）。
    抽取失败的**不写缓存**，下次会重试；避免 Word COM 一次失败被 [] 永久钉死。
    """
    out = []
    doc_stats: dict[str, dict] = {}
    n = 0
    for p in paths:
        if limit and n >= limit:
            break
        rel = str(p.relative_to(config.CORPUS_ROOT)) if config.CORPUS_ROOT in p.parents else str(p)
        cached, key = cache.get(p)
        text_chars = 0
        if isinstance(cached, dict):
            qs = [list(x) for x in cached.get("questions", [])]
            text_chars = int(cached.get("text_chars") or 0)
            stats["cache_hit"] += 1
        else:
            try:
                if p.suffix.lower() in (".md", ".txt"):
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
                continue              # 不缓存失败结果，下一轮重试
            text_chars = len(text)
            qs = [list(x) for x in split_qa(text)] if text else []
            cache.put(key, {"questions": qs, "text_chars": text_chars})
        stats["docs"] += 1
        stats["questions"] += len(qs)
        q_chars = 0
        n_q = 0
        for item in qs:
            q, a = (item, "") if isinstance(item, str) else (item[0], item[1] if len(item) > 1 else "")
            # 与 split_questions 同口径：非"像问题"的块（长段正文/陈述句）不进聚类，
            # 否则答案正文会被当成题干互相强并，制造假重复
            q = clean_question(q) or ""
            if not q:
                stats["not_question"] += 1
                continue
            nq = normalize(q)
            if len(nq) < 6:
                stats["too_short"] += 1
                continue
            q_chars += len(q)
            n_q += 1
            out.append((nq, q, rel, normalize(a) if a else ""))
        doc_stats[rel] = {"text_chars": text_chars, "q_chars": q_chars, "n_q": n_q}
        n += 1
    return out, doc_stats


def _pool_answer_covers(ans99: str, anspool: str) -> bool:
    """资源池答案能否覆盖 99 区答案（删除方向的单向校验）。

    99 有实质答案而资源池没有（标记识别失败/资源池缺答案）→ 不能覆盖，视为冲突；
    两边都有答案才比相似度。99 本身没有答案时没有"丢失答案"的风险，返回 True。
    """
    if len(ans99) < 8:
        return True
    if len(anspool) < 8:
        return False
    return jaccard(grams(ans99), grams(anspool)) >= ANSWER_AGREE


def _answer_conflict(ans99: str, anspool: str) -> bool:
    return not _pool_answer_covers(ans99, anspool)


def build_clusters(items, auto: float, manual: float, answers=None):
    """items = [(norm, simhash, rel)] → (dsu, pending_pairs, strong_pairs, conflicts)。

    强并条件：精确同文 或 (LSH 候选 & 汉明<=24 & Jaccard>=auto)。
    0.85<=J<0.92 只记 pending，不 union。
    conflicts：被强并但**答案不同**的对（不撤销并簇，只在 99 区判定时降级为 manual）。
    局限：答案块靠"答案：/解析：/答："这类标记切分，标记缺失时答案为空，冲突检查不生效；
    结果里的 answer_coverage 就是这条判据的覆盖率，低于 30% 会告警，此时 strong 需人工复核。
    """
    hashes = [simhash(t) for t, _, _ in items]
    gsets = [grams(t) for t, _, _ in items]
    dsu = DSU(len(items))
    buckets = defaultdict(list)
    for idx, h in enumerate(hashes):
        for band in range(8):                      # 8×8bit 分带：比 4×16 召回高（差 6 位的近重复对也能撞带）
            buckets[(band, (h >> (band * 8)) & 0xFF)].append(idx)
    pending, strong, conflicts = [], [], []
    seen_pair = set()
    conflict_seen = set()

    def check_answers(a: int, b: int) -> None:
        pk = (min(a, b), max(a, b))
        if pk in conflict_seen:
            return
        if answers and _answer_conflict(answers[a], answers[b]):
            conflict_seen.add(pk)
            conflicts.append({"pool_idx": pk[0], "q99_idx": pk[1],
                              "q": items[pk[0]][0][:120]})

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
                    check_answers(a, b)
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
            check_answers(idxs[0], k)
    return dsu, pending, strong, conflicts


MIN_MATCH_RATIO = 0.90       # 99 件强判：直接匹配题占比下限
MIN_TEXT_COVERAGE = 0.10     # 题块字符 / 原文可见字符；过低说明切题没覆盖正文
POOL_OK_STATUSES = {"classified", "processing", "processed"}
POOL_BAD_GATES = {"garbled", "empty_docx"}
LSH_MAX_HAMMING = 12         # J>=0.92 的 SimHash 通常只差 3~6 位；24 太松，候选爆炸


def load_pool_status() -> dict[str, dict]:
    """资源池文件的账本状态：源 rel 与 20-题库MD 的 md_rel 都映射到源记录。

    只有 processed/classified/processing 且 gate 不是乱码/空文档的样本，才有资格作为
    "可删见证"；rejected/superseded/quarantined 的池样本一律不能支撑删除。
    """
    db = Path(config.DB_PATH)
    if not db.exists():
        return {}
    import sqlite3
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    by_rel: dict[str, dict] = {}
    status_of = {r["doc_hash"]: {"status": r["status"], "gate": r["gate_result"]}
                 for r in conn.execute("SELECT doc_hash, status, gate_result FROM source_documents")}
    for r in conn.execute("SELECT dir_path, file_name, status, gate_result FROM source_documents"):
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        by_rel[rel.replace("\\", "/")] = {"status": r["status"], "gate": r["gate_result"]}
    try:
        for r in conn.execute("SELECT doc_hash, md_rel FROM document_products"):
            src = status_of.get(r["doc_hash"])
            if not src or not r["md_rel"]:
                continue
            md_rel = f"{DIR20}/{r['md_rel'].replace(chr(92), '/')}"
            by_rel[md_rel] = {"status": src["status"], "gate": src["gate"]}
    except sqlite3.OperationalError:
        pass
    conn.close()
    return by_rel


def pool_status_ok(info: dict | None) -> bool:
    return bool(info) and info.get("status") in POOL_OK_STATUSES \
        and (info.get("gate") or "") not in POOL_BAD_GATES


def direct_pool_matches(items, n_pool: int, answers, pool_status: dict,
                        auto: float, manual: float) -> dict[int, dict]:
    """每条 99 题直接和资源池题比对（不走 single-linkage 传递闭包）。

    返回 {q99_idx: {"strong": [...], "pending": [...], "invalid": [...], "conflict": [...]}}；
    strong 里每一项都带 direct Jaccard，且池样本状态合法。
    """
    hashes = [simhash(t) for t, _, _ in items]
    gsets = [grams(t) for t, _, _ in items]
    buckets = defaultdict(list)
    exact = defaultdict(list)
    for i in range(n_pool):
        exact[items[i][0]].append(i)
        for band in range(8):
            buckets[(band, (hashes[i] >> (band * 8)) & 0xFF)].append(i)
    out: dict[int, dict] = {}
    for i in range(n_pool, len(items)):
        cands = set(exact.get(items[i][0], []))
        for band in range(8):
            cands.update(buckets.get((band, (hashes[i] >> (band * 8)) & 0xFF), []))
        rec = {"strong": [], "pending": [], "invalid": [], "conflict": []}
        for p in sorted(cands):
            if hamming(hashes[i], hashes[p]) > LSH_MAX_HAMMING:
                continue
            jac = jaccard(gsets[i], gsets[p])
            if jac >= auto:
                if not pool_status_ok(pool_status.get(items[p][2].replace(chr(92), "/"))):
                    rec["invalid"].append({"pool_idx": p, "jac": round(jac, 4)})
                elif _answer_conflict(answers[i], answers[p]):
                    rec["conflict"].append({"pool_idx": p, "jac": round(jac, 4)})
                else:
                    rec["strong"].append({"pool_idx": p, "jac": round(jac, 4)})
            elif jac >= manual:
                rec["pending"].append({"pool_idx": p, "jac": round(jac, 4)})
        out[i] = rec
    return out
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题级吸附 + 99 区 gate")
    ap.add_argument("--auto", type=float, default=0.92, help="强并 Jaccard 阈值")
    ap.add_argument("--manual", type=float, default=0.85, help="待人工下限")
    ap.add_argument("--sample", type=int, default=50, help="强簇抽样数（人工核）")
    ap.add_argument("--limit", type=int, default=0,
                    help="每类最多处理 N 件（调试；结果标 complete=false，默认不覆盖全量结果）")
    ap.add_argument("--no-cache", action="store_true", help="忽略抽取缓存")
    ap.add_argument("--out", default=None, help="结果路径（默认 build/cluster_result.json）")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else config.BUILD_DIR / "cluster_result.json"
    if args.limit and not args.out and out.exists():
        out = config.BUILD_DIR / f"cluster_result_limit{args.limit}.json"

    R = config.CORPUS_ROOT
    t0 = time.perf_counter()
    stats: defaultdict = defaultdict(int)

    pool_paths = [p for d in POOL_TOPS for p in iter_docs(R / d)]
    pool_paths += list(iter_docs(R / DIR20, md_only=True))
    docs99 = sorted(iter_docs(R / DIR99))

    cache = TextCache(config.BUILD_DIR / "cluster_text_cache.json", not args.no_cache)
    print(f"[pool] 库内 {len(pool_paths)} 件，99 区 {len(docs99)} 件", flush=True)
    try:
        pool_q, _pool_docs = collect_questions(pool_paths, cache, stats, args.limit)
        q99, q99_docs = collect_questions(docs99, cache, stats, args.limit)
    finally:
        close_word()        # 异常/中断也必须释放 Word COM
        cache.save()

    all_q = pool_q + q99
    items = [(t, simhash(t), rel) for (t, _q, rel, _a) in all_q]
    answers = [a for (_t, _q, _r, a) in all_q]
    # 答案覆盖率：没有答案块的题无法做"资源池答案是否覆盖 99 答案"判定，需逐件看
    answer_coverage = round(sum(1 for a in answers if a) / len(answers), 4) if answers else 0.0
    n_pool = len(pool_q)
    print(f"[items] 库内题 {n_pool}，99 区题 {len(q99)}，共 {len(items)}，"
          f"答案覆盖 {answer_coverage:.0%}", flush=True)
    if items and answer_coverage < 0.3:
        print("[warn] 答案块覆盖不足 30%：跨库答案覆盖判定基本失效，99 区 strong 结论需人工复核",
              file=sys.stderr)

    pool_status = load_pool_status()
    direct = direct_pool_matches(items, n_pool, answers, pool_status, args.auto, args.manual)
    dsu, pending, strong, conflicts = build_clusters(items, args.auto, args.manual, answers)

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

    # 99 区逐件判定：每条 99 题只认**直接** Jaccard>=auto 的池样本，且池样本状态合法；
    # 文件级再卡匹配占比、答案覆盖与文本覆盖，避免"一条命中、整份可删"。
    pending_by_99item: dict[str, list] = defaultdict(list)
    conflict_by_99item: dict[str, list] = defaultdict(list)
    answer_conflicts = []
    per_file: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "matched": 0, "pending": 0, "none": 0,
                 "invalid": 0, "conflicts": 0, "answers": 0})
    for i in range(n_pool, len(items)):
        rel = items[i][2]
        rec = direct.get(i, {"strong": [], "pending": [], "invalid": [], "conflict": []})
        f = per_file[rel]
        f["total"] += 1
        if answers[i]:
            f["answers"] += 1
        if rec["conflict"]:
            f["conflicts"] += 1
            for m in rec["conflict"]:
                p = m["pool_idx"]
                record = {"pool_rel": items[p][2], "pool_rel_q": items[p][0][:120],
                          "q99": items[i][0][:120], "jac": m["jac"],
                          "why": "pool_answer_missing_or_differs"}
                conflict_by_99item[rel].append(record)
                answer_conflicts.append(record)
        elif rec["strong"]:
            f["matched"] += 1
        elif rec["invalid"]:
            f["invalid"] += 1
        elif rec["pending"]:
            f["pending"] += 1
            for m in rec["pending"]:
                p = m["pool_idx"]
                pending_by_99item[rel].append({
                    "pool_rel": items[p][2], "pool_rel_q": items[p][0][:120],
                    "q99": items[i][0][:120], "jac": m["jac"]})
        else:
            f["none"] += 1

    verdicts = []
    cnt = defaultdict(int)
    first_q99_by_rel: dict[str, int] = {}
    for i in range(n_pool, len(items)):
        first_q99_by_rel.setdefault(items[i][2], i)
    for p in docs99:
        rel = str(p.relative_to(config.CORPUS_ROOT)) if config.CORPUS_ROOT in p.parents else str(p)
        f = per_file.get(rel)
        info = q99_docs.get(rel) or {}
        if not f or f["total"] == 0:
            verdicts.append({"rel": rel, "verdict": "none", "cluster_size": 0,
                             "total_questions": 0, "matched": 0, "match_ratio": 0.0,
                             "pending": [], "answer_conflicts": [],
                             "reasons": ["no_questions"]})
            cnt["none"] += 1
            continue
        ratio = f["matched"] / f["total"]
        text_chars = info.get("text_chars") or 0
        text_cov = round((info.get("q_chars") or 0) / text_chars, 4) if text_chars else 0.0
        answer_cov = round(f["answers"] / f["total"], 4)
        reasons = []
        if ratio < MIN_MATCH_RATIO:
            reasons.append(f"match_ratio {ratio:.2f}<{MIN_MATCH_RATIO}")
        if f["pending"]:
            reasons.append(f"pending {f['pending']}")
        if f["conflicts"]:
            reasons.append(f"answer_conflicts {f['conflicts']}")
        if f["invalid"]:
            reasons.append(f"pool_status_invalid {f['invalid']}")
        if f["answers"] == 0:
            reasons.append("no_answers")
        if text_cov < MIN_TEXT_COVERAGE:
            reasons.append(f"text_coverage {text_cov:.2f}<{MIN_TEXT_COVERAGE}")
        strong_ok = not reasons
        v = "strong" if strong_ok else ("manual" if f["matched"] or f["pending"]
                                        or f["conflicts"] or f["invalid"] else "none")
        verdicts.append({
            "rel": rel, "verdict": v,
            "cluster_size": (len(groups[dsu.find(first_q99_by_rel[rel])])
                             if rel in first_q99_by_rel else 0),
            "total_questions": f["total"], "matched": f["matched"],
            "match_ratio": round(ratio, 4), "answer_coverage": answer_cov,
            "text_coverage": text_cov,
            "pending": pending_by_99item.get(rel, []),
            "answer_conflicts": conflict_by_99item.get(rel, []),
            "reasons": reasons,
        })
        cnt[v] += 1

    # 强簇抽样（人工核准确率）：必须含 99 区成员，纯资源池簇对删除闸门没有检验价值
    sample_pool = [c for c in clusters_out if c["n99"] > 0]
    sample = random.Random(42).sample(sample_pool, min(args.sample, len(sample_pool))) \
        if sample_pool else []

    result = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "complete": args.limit == 0,
        "limit": args.limit,
        "errors": stats["error"],
        "thresholds": {"auto": args.auto, "manual": args.manual},
        "stats": {**stats, "pool_questions": n_pool, "q99_questions": len(q99),
                  "clusters_ge2_with_pool": len(clusters_out),
                  "answer_coverage": answer_coverage,
                  "direct_strong_pairs": sum(len(r["strong"]) for r in direct.values()),
                  "strong_pairs": len(strong), "pending_pairs": len(pending),
                  "answer_conflicts": len(answer_conflicts)},
        "verdict99": dict(cnt),
        "answer_conflicts": answer_conflicts,
        "verdict_detail": verdicts,
        "clusters_top": clusters_out[:300],
        "sample": sample,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)       # 原子落盘：消费方不会读到半个 JSON

    suffix = "" if result["complete"] else f"_limit{args.limit}"
    pend_path = config.BUILD_DIR / f"cluster_pending{suffix}.csv"
    with pend_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rel99", "q99", "pool_rel", "pool_q", "jaccard"])
        for rel99, pairs in pending_by_99item.items():
            for pp in pairs:
                w.writerow([rel99, pp["q99"], pp["pool_rel"], pp["pool_rel_q"], pp["jac"]])

    sample_path = config.BUILD_DIR / f"cluster_sample50{suffix}.md"
    lines = ["# cluster 强簇抽样（人工核准确率用）", "",
             f"- 阈值 auto={args.auto} / manual={args.manual}；抽样 {len(sample)}/{len(clusters_out)} 簇",
             "- 核法：看同簇成员的题是否真是同一题；误并请记簇内两行题文", ""]
    for k, c in enumerate(sample, 1):
        lines.append(f"## 簇 {k}（{c['size']} 成员，99 区 {c['n99']}）")
        for m in c["members"][:8]:
            tag = "【99】" if m["from99"] else "【库】"
            lines.append(f"- {tag} `{Path(m['rel']).name}`：{m['q']}")
        if len(c["members"]) > 8:
            lines.append(f"- …共 {c['size']} 成员")
        lines.append("")
    sample_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({
        "docs": stats["docs"], "questions": stats["questions"],
        "clusters_ge2_with_pool": len(clusters_out),
        "answer_coverage": answer_coverage,
        "direct_strong_pairs": sum(len(r["strong"]) for r in direct.values()),
        "strong_pairs": len(strong), "pending_pairs": len(pending),
        "answer_conflicts": len(answer_conflicts),
        "verdict99": dict(cnt),
        "complete": result["complete"], "limit": result["limit"],
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }, ensure_ascii=False, indent=1))
    print(f"[out] {out}\n[out] {pend_path}\n[out] {sample_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
