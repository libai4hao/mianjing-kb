# -*- coding: utf-8 -*-
"""canonical：questions 表 → 题簇落库（canonical_questions / question_variants）。

「题级吸附」的存储层（docs/03 §10.2 canonical 表，2026-11-17 落地）：
  - canonical_questions  一行 = 一个"同一道题"（近重复簇），witness = 见证来源数
                          （distinct collection；collection 为空回退 doc_hash，Q-2 2026-11-19）
  - question_variants    一行 = 该题的一个变体（源 doc_hash + seq），FK 级联
聚类复用 report.build_clusters（2-gram 倒排 + Jaccard 精确判定，P1-4 同款）；
单件簇也落库——canonical 是全量唯一题登记簿，不只存重复簇。

全量重建的边界（2026-11-19 修复）：
  - 清空的是 question_variants / canonical_questions / canonical_dependents 三张派生表；
    merge_candidates 保留，人工 approve/reject 结果不会因重建丢失。
  - canonical_id 采用"粘性绑定"：新簇优先沿用成员最多的旧簇 ID；旧簇被合并/拆分时，
    通过成员重叠把旧 ID 映射到新簇，避免 bookmarks/备注/未来向量表静默悬空。
  - 旧 merge_candidates 的 canonical_id 按上述映射回写；目标簇确实消失的候选保留原状态，
    由 --candidates 暴露，不静默删除。

阈值口径（2026-11-19 修复）：
  - 全量 build_clusters 的 --jac 0.7 是权威基准；
  - 增量是两次全量之间的过渡方案，默认 --attach-jac 0.7 与全量一致；
    --candidate-jac 0.65–0.7 只登记人工复核候选，不自动挂靠。
    增量不再使用 0.92 的另一套口径，避免把全量本可合并的簇拆碎。

用法：
    python -m mianjing_kb.canonical            # 全量重建（派生表重建，保留人工候选）
    python -m mianjing_kb.canonical --stats    # 只看落库分布
    python -m mianjing_kb.canonical --incremental
    python -m mianjing_kb.canonical --candidates
    python -m mianjing_kb.canonical --approve <q_key> [--approve ...]
    python -m mianjing_kb.canonical --apply-candidates
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import manifests  # noqa: E402
from mianjing_kb.kb import config, db, wlock  # noqa: E402
from mianjing_kb.report import build_clusters, load_questions  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
TITLE_MAX = 500
ATTACH_JAC = 0.7        # 权威口径：与全量 --jac 默认一致
CANDIDATE_JAC = 0.65    # 只登记人工复核，不自动挂靠
FILTER_JAC = min(CANDIDATE_JAC, ATTACH_JAC)   # 倒排前缀按更低阈值建，保证 0.7 候选不丢


def _prefix_grams(g: frozenset[str], min_jac: float) -> list[str]:
    """前缀过滤：字典序全局一致，Jaccard>=t 的两集合必有共享 token 落在双方前缀。"""
    if not g:
        return []
    k = max(1, len(g) - math.ceil(min_jac * len(g)) + 1)
    return sorted(g)[:k]


def variant_id(q_key: str) -> str:
    """变体主键：sha256("variant:" + q_key)[:16]。

    q_key=(doc_hash,q_norm_hash) 才是跨重切稳定的身份；seq 只是 chunker 输出序号，
    文档前部增删一题会让后面所有 seq 移位，不能做主键。
    """
    return hashlib.sha256(f"variant:{q_key}".encode("utf-8")).hexdigest()[:16]


def normalize_variant_ids(conn) -> int:
    """把历史 (doc_hash,seq) 公式的 variant_id 改成 q_key 公式。"""
    rows = conn.execute(
        "SELECT variant_id, canonical_id, q_key FROM question_variants "
        "WHERE COALESCE(q_key,'')<>''").fetchall()
    changed = 0
    for r in rows:
        old, cid, qkey = r["variant_id"], r["canonical_id"], r["q_key"]
        new = variant_id(qkey)
        if old == new:
            continue
        hit = conn.execute("SELECT canonical_id, q_key FROM question_variants "
                           "WHERE variant_id=?", (new,)).fetchone()
        if hit:
            # 新 ID 已被同一 q_key 的正常行占用：删除旧别名，避免主键冲突。
            conn.execute("DELETE FROM question_variants WHERE variant_id=?", (old,))
            if (hit["canonical_id"], hit["q_key"]) != (cid, qkey):
                conn.execute("DELETE FROM canonical_dependents WHERE canonical_id=? AND q_key=?",
                             (cid, qkey))
        else:
            conn.execute("UPDATE question_variants SET variant_id=? WHERE variant_id=?",
                         (new, old))
        changed += 1
    return changed


def prune_orphan_variants(conn) -> int:
    """删除 q_key 已不在 questions 表的 variant，并清掉其 dependent/witness。"""
    rows = conn.execute(
        "SELECT variant_id, canonical_id, q_key FROM question_variants "
        "WHERE COALESCE(q_key,'')='' OR q_key NOT IN (SELECT q_key FROM questions)").fetchall()
    touched = set()
    for r in rows:
        conn.execute("DELETE FROM question_variants WHERE variant_id=?", (r["variant_id"],))
        if r["q_key"]:
            conn.execute("DELETE FROM canonical_dependents WHERE canonical_id=? AND q_key=?",
                         (r["canonical_id"], r["q_key"]))
        touched.add(r["canonical_id"])
    for cid in touched:
        has = conn.execute("SELECT 1 FROM question_variants WHERE canonical_id=? LIMIT 1",
                           (cid,)).fetchone()
        if has:
            _recompute_witness(conn, cid)
        else:
            conn.execute("DELETE FROM canonical_dependents WHERE canonical_id=?", (cid,))
            conn.execute("DELETE FROM canonical_questions WHERE canonical_id=?", (cid,))
    return len(rows)


def _topic(value: str | None) -> str:
    """topic 统一归一化：空串/未分类都不投票，也不写进 canonical。"""
    v = (value or "").strip()
    return "" if v == config.UNCLASSIFIED else v


def _collection(value: str | None) -> str:
    """collection 统一归一化：纯空白视为空，避免 Python/SQL 两侧判定不一致。"""
    return (value or "").strip()


def collection_key(value: str | None, doc_hash: str) -> str:
    return _collection(value) or f"doc:{doc_hash}"


def doc_meta(conn) -> dict[str, tuple[str, str]]:
    """{doc_hash: (topic, collection)}——variant 的 collection/topic 归属，先归一化再入内存。

    没有 source_documents 行的孤儿 md 统一记成一个 `md-orphan` collection：
    否则每篇孤儿都会回退成自己的 doc:<hash>，把同一道题的见证数虚高。
    """
    meta = {r["doc_hash"]: (_topic(r["topic"]), _collection(r["collection"]))
            for r in conn.execute("SELECT doc_hash, topic, collection FROM source_documents")}
    for r in conn.execute(
            "SELECT DISTINCT q.doc_hash FROM questions q "
            "LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash WHERE sd.doc_hash IS NULL"):
        meta[r["doc_hash"]] = ("", "md-orphan")
    return meta


def _qkey(q: dict) -> str:
    return q.get("q_key") or f"{q['doc_hash']}:{q['q_norm_hash']}"


def _topic_vote(values: list[str]) -> str | None:
    topics = Counter(t for t in (_topic(v) for v in values) if t)
    return topics.most_common(1)[0][0] if topics else None


def _unique_cid(text: str, used: set[str]) -> str:
    base = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    cid = base
    n = 1
    while cid in used:
        cid = hashlib.sha256(f"{base}#{n}".encode("utf-8")).hexdigest()[:16]
        n += 1
    return cid


def build_rows(qs: list[dict], clusters: list[dict],
               meta: dict[str, tuple[str, str]], now: str, *,
               sticky: dict[str, str] | None = None,
               created_at: dict[str, str] | None = None,
               ) -> tuple[list[tuple], list[tuple], dict[str, str]]:
    """簇 → 两表行 + 旧 ID→新 ID 映射。

    canonical_id 优先沿用旧簇 ID（成员重叠最多者），无法沿用才按代表题干 norm 生成；
    topic = 成员文档 topic 的多数票（空/未分类不入票）。
    """
    sticky = sticky or {}
    created_at = created_at or {}
    rows_c: list[tuple] = []
    rows_v: list[tuple] = []
    used: set[str] = set()
    claimed: dict[str, str] = {}
    for c in clusters:
        norms = Counter(qs[i]["q_norm"] for i in c["members"])
        best = max(norms, key=lambda n: (norms[n], len(n)))
        old_ids: Counter[str] = Counter()
        for i in c["members"]:
            old = sticky.get(_qkey(qs[i]))
            if old:
                old_ids[old] += 1
        cid = None
        for old, _n in old_ids.most_common():
            if old not in used:
                cid = old
                used.add(old)
                break
        if cid is None:
            cid = _unique_cid(best, used)
            used.add(cid)
        for old in old_ids:
            claimed.setdefault(old, cid)
        topics = [_topic(meta.get(qs[i]["doc_hash"], ("", ""))[0]) for i in c["members"]]
        topic = _topic_vote(topics)
        # Q-2（2026-11-19）：见证数 = distinct collection；collection 为空回退到 doc_hash
        witness = len({collection_key(meta.get(qs[i]["doc_hash"], ("", ""))[1], qs[i]["doc_hash"])
                       for i in c["members"]})
        rows_c.append((cid, best[:TITLE_MAX], topic, None, witness,
                       created_at.get(cid, now), now))
        for i in c["members"]:
            q = qs[i]
            rows_v.append((variant_id(_qkey(q)), cid, q["doc_hash"],
                           str(q["seq"]), (q.get("a_text") or "")[:2000],
                           _collection(meta.get(q["doc_hash"], ("", ""))[1]), now,
                           _qkey(q)))
    aliases = {old: new for old, new in claimed.items() if old != new}
    return rows_c, rows_v, aliases


def write_all(conn, rows_c: list[tuple], rows_v: list[tuple],
              aliases: dict[str, str] | None = None) -> None:
    """重建三张派生表；merge_candidates 不清空，人工审核结果保留。

    canonical_dependents 可从 variants 重建，所以清空是安全的；merge_candidates 不是派生表，
    清空会丢 approve/reject，必须保留。
    """
    conn.execute("DELETE FROM question_variants")
    conn.execute("DELETE FROM canonical_questions")
    conn.execute("DELETE FROM canonical_dependents")
    conn.executemany(
        "INSERT INTO canonical_questions "
        "(canonical_id, title_norm, topic, center_vec, witness, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)", rows_c)
    conn.executemany(
        "INSERT INTO question_variants "
        "(variant_id, canonical_id, doc_hash, question_no, answering_text, collection, "
        " created_at, q_key) VALUES (?,?,?,?,?,?,?,?)", rows_v)
    # T29：dependent 是全量 rebuild 与增量 attach 共用的见证来源；witness 重算只扫本簇。
    conn.execute(
        "INSERT OR IGNORE INTO canonical_dependents(canonical_id,q_key,doc_hash,collection) "
        "SELECT canonical_id, q_key, doc_hash, COALESCE(collection,'') "
        "FROM question_variants WHERE COALESCE(q_key,'')!=''")
    # 旧簇被合并/拆分时，把候选指向的旧 ID 回写到新簇；目标消失的候选保留原状，由列表暴露。
    new_ids = {r[0] for r in rows_c}
    now = datetime.now().strftime(NOW_FMT)
    for old, new in (aliases or {}).items():
        if old == new or new not in new_ids:
            continue
        conn.execute("UPDATE merge_candidates SET canonical_id=?, updated_at=? "
                     "WHERE canonical_id=?", (new, now, old))


@lru_cache(maxsize=20000)
def _grams(text: str) -> frozenset[str]:
    s = " ".join((text or "").split()).lower()
    return frozenset(s[i:i + 2] for i in range(max(0, len(s) - 1)))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(a: str, b: str) -> float:
    """字符 2-gram Jaccard；相同题干=1，空串=0。"""
    return jaccard(_grams(a), _grams(b))


def _recompute_witness(conn, canonical_id: str) -> int:
    n = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(collection),''), 'doc:'||doc_hash)) "
        "FROM canonical_dependents WHERE canonical_id=?", (canonical_id,)).fetchone()[0]
    conn.execute("UPDATE canonical_questions SET witness=?, updated_at=? WHERE canonical_id=?",
                 (n, datetime.now().strftime(NOW_FMT), canonical_id))
    return int(n)


def _recompute_topic(conn, canonical_id: str,
                     meta: dict[str, tuple[str, str]]) -> str | None:
    """按当前全部成员重算 topic；挂靠新变体后不能让 UNCLASSIFIED 留在簇上。"""
    topics = []
    for row in conn.execute("SELECT doc_hash FROM question_variants WHERE canonical_id=?",
                            (canonical_id,)):
        topics.append(_topic(meta.get(row["doc_hash"], ("", ""))[0]))
    topic = _topic_vote(topics)
    conn.execute("UPDATE canonical_questions SET topic=?, updated_at=? WHERE canonical_id=?",
                 (topic, datetime.now().strftime(NOW_FMT), canonical_id))
    return topic


def _rep_index(reps: list[tuple[str, str]]):
    """预计算代表题干的 gram 与前缀倒排，供增量候选召回。

    前缀按 FILTER_JAC（0.65）建：它比挂靠阈值 0.7 更低，0.7 的候选必然落在
    0.65 的候选集里；不再保留 SimHash 16bit 分带（会漏掉汉明距离大的同义改写）。
    """
    by_norm: dict[str, str] = {}
    gram_sets: dict[str, frozenset[str]] = {}
    index: dict[str, list[str]] = defaultdict(list)
    for cid, title in reps:
        key = (title or "")[:TITLE_MAX]
        by_norm.setdefault(key, cid)
        g = _grams(title or "")
        gram_sets[cid] = g
        for token in _prefix_grams(g, FILTER_JAC):
            index[token].append(cid)
    return by_norm, gram_sets, index


def _add_rep(cid: str, title: str, by_norm, gram_sets, index) -> None:
    key = (title or "")[:TITLE_MAX]
    by_norm.setdefault(key, cid)
    g = _grams(title or "")
    gram_sets[cid] = g
    for token in _prefix_grams(g, FILTER_JAC):
        index[token].append(cid)


def _best_existing(norm: str, by_norm, gram_sets, index) -> tuple[str | None, float]:
    key = norm[:TITLE_MAX]
    if key in by_norm:
        return by_norm[key], 1.0
    qg = _grams(norm)
    cands: set[str] = set()
    for token in _prefix_grams(qg, FILTER_JAC):
        cands.update(index.get(token, ()))
    best_cid, best_sim = None, 0.0
    for cid in cands:
        s = jaccard(qg, gram_sets.get(cid, frozenset()))
        if s > best_sim:
            best_cid, best_sim = cid, s
    return best_cid, best_sim


def _write_variant(conn, q: dict, cid: str, qkey: str, now: str,
                   meta: dict[str, tuple[str, str]]) -> tuple[str, str | None]:
    """写/替换一个 variant；variant_id 冲突时显式处理，不静默 IGNORE。

    返回 (status, old_canonical_id)。status = new / replaced / same。
    """
    vid = variant_id(qkey)
    row = conn.execute(
        "SELECT canonical_id, q_key FROM question_variants WHERE variant_id=?", (vid,)).fetchone()
    collection = _collection(meta.get(q["doc_hash"], ("", ""))[1])
    if row is None:
        conn.execute(
            "INSERT INTO question_variants"
            "(variant_id,canonical_id,doc_hash,question_no,answering_text,collection,created_at,q_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (vid, cid, q["doc_hash"], str(q["seq"]), (q.get("a_text") or "")[:2000],
             collection, now, qkey))
        return "new", None
    old_cid, old_qkey = row["canonical_id"], row["q_key"] or ""
    if old_qkey and (old_qkey != qkey or old_cid != cid):
        # q_key 变了或变体改挂别的簇：旧 dependent 必须先撤，否则见证数会双算。
        conn.execute("DELETE FROM canonical_dependents WHERE canonical_id=? AND q_key=?",
                     (old_cid, old_qkey))
    if old_cid != cid or old_qkey != qkey:
        conn.execute(
            "UPDATE question_variants SET canonical_id=?, answering_text=?, collection=?, "
            "q_key=?, created_at=? WHERE variant_id=?",
            (cid, (q.get("a_text") or "")[:2000], collection, qkey, now, vid))
        return "replaced", old_cid
    conn.execute("UPDATE question_variants SET answering_text=?, collection=? WHERE variant_id=?",
                 ((q.get("a_text") or "")[:2000], collection, vid))
    return "same", old_cid


def attach_incremental(conn, attach_jac: float = ATTACH_JAC,
                       candidate_jac: float = CANDIDATE_JAC,
                       batch_id: str | None = None) -> dict:
    """T29：只处理尚未进 variant 的新题，吸附/候选/新建三路，不重跑全量聚类。

    - q_key 已在 question_variants 的题跳过（幂等）；
    - 用预计算 gram + 前缀倒排找候选，避免 O(新题 × 全部 canonical) 的集合重建；
    - Jaccard ≥ attach_jac → 吸到最相似 canonical；
    - candidate_jac ≤ sim < attach_jac → merge_candidates(status=pending)，不落 variant；
    - 其余 → 新 canonical + variant；
    - 新 q_key 正常新建变体；旧 q_key 的变体由入口清理，不再按 (doc_hash,seq) 复用 ID；
    - 只重算 touched canonical 的 witness/topic。
    """
    now = datetime.now().strftime(NOW_FMT)
    normalize_variant_ids(conn)
    prune_orphan_variants(conn)
    attached_keys = {r[0] for r in conn.execute(
        "SELECT q_key FROM question_variants WHERE COALESCE(q_key,'')!=''")}
    reps = [(r[0], r[1]) for r in conn.execute(
        "SELECT canonical_id, title_norm FROM canonical_questions")]
    by_norm, gram_sets, index = _rep_index(reps)
    known = set(by_norm.values())
    meta = doc_meta(conn)
    created: list[str] = []
    created_set: set[str] = set()
    seeded: set[str] = set()
    attached: list[str] = []
    replaced: list[str] = []
    added_variants: list[str] = []
    candidates: list[dict] = []
    touched: set[str] = set()

    for q in load_questions(conn):
        qkey = _qkey(q)
        if qkey in attached_keys:
            continue
        norm = q.get("q_norm") or ""
        best_cid, best_sim = _best_existing(norm, by_norm, gram_sets, index)
        if best_cid and best_sim < attach_jac and best_sim >= candidate_jac:
            conn.execute(
                "INSERT INTO merge_candidates(q_key,canonical_id,similarity,status,created_at,updated_at) "
                "VALUES(?,?,?, 'pending', ?,?) ON CONFLICT(q_key) DO UPDATE SET "
                "canonical_id=excluded.canonical_id, similarity=excluded.similarity, "
                "updated_at=excluded.updated_at "
                "WHERE merge_candidates.status='pending'",
                (qkey, best_cid, round(best_sim, 4), now, now))
            candidates.append({"q_key": qkey, "canonical_id": best_cid,
                               "similarity": round(best_sim, 4)})
            continue

        if not best_cid or best_sim < attach_jac:
            cid = _unique_cid(norm, known)
            topic = _topic(meta.get(q["doc_hash"], ("", ""))[0]) or _topic(q.get("topic"))
            conn.execute(
                "INSERT INTO canonical_questions"
                "(canonical_id,title_norm,topic,center_vec,witness,created_at,updated_at) "
                "VALUES(?,?,?,NULL,0,?,?)",
                (cid, norm[:TITLE_MAX], topic, now, now))
            created.append(cid)
            created_set.add(cid)
            known.add(cid)
            _add_rep(cid, norm, by_norm, gram_sets, index)
            best_cid, best_sim = cid, 1.0

        cid = best_cid
        kind, old_cid = _write_variant(conn, q, cid, qkey, now, meta)
        conn.execute(
            "INSERT OR IGNORE INTO canonical_dependents(canonical_id,q_key,doc_hash,collection) "
            "VALUES(?,?,?,?)",
            (cid, qkey, q["doc_hash"], _collection(meta.get(q["doc_hash"], ("", ""))[1])))
        if kind == "new":
            if cid in created_set and cid not in seeded:
                seeded.add(cid)          # 新簇的第一条：计入 created，而不是 attached
            else:
                attached.append(qkey)
        elif kind == "replaced":
            replaced.append(qkey)
            attached.append(qkey)
        else:
            attached.append(qkey)
        if old_cid:
            touched.add(old_cid)
        added_variants.append(qkey)
        touched.add(cid)
        attached_keys.add(qkey)

    for cid in touched:
        _recompute_topic(conn, cid, meta)
        _recompute_witness(conn, cid)
    payload = {"tool": "canonical-incremental",
               "created_canonicals": created,
               "attached": attached, "replaced_variants": replaced,
               "candidates": candidates, "added_variants": added_variants,
               "touched_canonicals": sorted(touched)}
    if batch_id:
        payload["manifest"] = str(manifests.create(batch_id, payload).get("batch_id"))
    return {"new_questions": len(added_variants) + len(candidates),
            "created_canonicals_count": len(created),
            "created_questions": len(seeded),
            "attached_count": len(attached),
            "replaced_variants_count": len(replaced),
            "candidates_count": len(candidates),
            "added_variants_count": len(added_variants),
            "batch_id": batch_id, **payload}


def review_candidates(conn, qkeys: list[str], status: str) -> dict:
    """人工设置候选状态；只允许 approved/rejected。"""
    if status not in ("approved", "rejected"):
        raise ValueError(f"非法候选状态: {status}")
    now = datetime.now().strftime(NOW_FMT)
    rows = []
    for qkey in qkeys:
        cur = conn.execute("UPDATE merge_candidates SET status=?, updated_at=? WHERE q_key=?",
                           (status, now, qkey))
        rows.append({"q_key": qkey, "updated": bool(cur.rowcount)})
    return {"status": status, "rows": rows,
            "updated": sum(1 for r in rows if r["updated"])}


def apply_candidates(conn, meta: dict[str, tuple[str, str]] | None = None) -> dict:
    """消费 approved 候选：真正挂到目标 canonical，状态转 merged。

    这是 0.65–0.7（原 0.85–0.92）候选区间的审核出口；没有它，merge_candidates 只会膨胀。
    """
    now = datetime.now().strftime(NOW_FMT)
    normalize_variant_ids(conn)
    prune_orphan_variants(conn)
    meta = meta or doc_meta(conn)
    by_key = {_qkey(q): q for q in load_questions(conn)}
    rows = conn.execute(
        "SELECT q_key, canonical_id FROM merge_candidates WHERE status='approved' "
        "ORDER BY similarity DESC, q_key").fetchall()
    applied, skipped, touched = [], [], set()
    for row in rows:
        qkey, cid = row["q_key"], row["canonical_id"]
        q = by_key.get(qkey)
        exists = conn.execute("SELECT 1 FROM canonical_questions WHERE canonical_id=?",
                              (cid,)).fetchone()
        if q is None or not exists:
            skipped.append({"q_key": qkey, "why": "question 或 canonical 已不存在"})
            continue
        kind, old_cid = _write_variant(conn, q, cid, qkey, now, meta)
        conn.execute(
            "INSERT OR IGNORE INTO canonical_dependents(canonical_id,q_key,doc_hash,collection) "
            "VALUES(?,?,?,?)",
            (cid, qkey, q["doc_hash"], _collection(meta.get(q["doc_hash"], ("", ""))[1])))
        conn.execute("UPDATE merge_candidates SET status='merged', updated_at=? WHERE q_key=?",
                     (now, qkey))
        applied.append({"q_key": qkey, "canonical_id": cid, "variant": kind})
        touched.add(cid)
        if old_cid:
            touched.add(old_cid)
    for cid in touched:
        _recompute_topic(conn, cid, meta)
        _recompute_witness(conn, cid)
    return {"applied": applied, "skipped": skipped,
            "applied_count": len(applied), "skipped_count": len(skipped)}


def _run(args) -> int:
    readonly = args.dry_run or args.stats or args.candidates
    src = None
    if args.incremental and args.dry_run:
        # 增量 dry-run 需要写内存库才能算吸附；绝不在原库上开 SAVEPOINT/写锁。
        src = db.connect(args.db, create=False, readonly=True)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        src.backup(conn)
        src.close()
        src = None
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn = db.connect(args.db, create=not readonly, readonly=readonly)

    if args.candidates:
        out = [dict(r) for r in conn.execute(
            "SELECT q_key, canonical_id, similarity, status, created_at, updated_at "
            "FROM merge_candidates ORDER BY status='pending' DESC, similarity DESC, q_key")]
        print(json.dumps({"count": len(out), "candidates": out},
                         ensure_ascii=False, indent=1))
        conn.close()
        return 0

    if args.approve or args.reject:
        out = {}
        if args.approve:
            out["approved"] = review_candidates(conn, args.approve, "approved")
        if args.reject:
            out["rejected"] = review_candidates(conn, args.reject, "rejected")
        conn.commit()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    if args.apply_candidates:
        out = apply_candidates(conn)
        conn.commit()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    if args.stats:
        out = {
            "canon": conn.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0],
            "variants": conn.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0],
            "multi_doc": conn.execute(
                "SELECT COUNT(*) FROM canonical_questions WHERE witness >= 2").fetchone()[0],
            "merge_pending": conn.execute(
                "SELECT COUNT(*) FROM merge_candidates WHERE status='pending'").fetchone()[0],
            "topic_top10": dict(conn.execute(
                "SELECT COALESCE(topic, '<null>') AS t, COUNT(*) AS n FROM canonical_questions "
                "GROUP BY 1 ORDER BY n DESC LIMIT 10").fetchall()),
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    if args.incremental:
        started = time.perf_counter()
        batch_id = args.batch_id or datetime.now().strftime("inc-%Y%m%d-%H%M%S")
        out = attach_incremental(conn, args.attach_jac, args.candidate_jac,
                                 None if args.dry_run else batch_id)
        if not args.dry_run:
            conn.commit()
        out["dry_run"] = bool(args.dry_run)
        out["elapsed_sec"] = round(time.perf_counter() - started, 1)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    started = time.perf_counter()
    qs = load_questions(conn)
    clusters = build_clusters(qs, args.dist, args.jac)
    sticky = {r[0]: r[1] for r in conn.execute(
        "SELECT q_key, canonical_id FROM question_variants WHERE COALESCE(q_key,'')!=''")}
    old_created = {r[0]: r[1] for r in conn.execute(
        "SELECT canonical_id, created_at FROM canonical_questions")}
    rows_c, rows_v, aliases = build_rows(
        qs, clusters, doc_meta(conn), datetime.now().strftime(NOW_FMT),
        sticky=sticky, created_at=old_created)
    if not args.dry_run:
        write_all(conn, rows_c, rows_v, aliases)
        conn.commit()
    multi_source = sum(1 for c in clusters if c["n_collections"] >= 2)
    multi_doc = sum(1 for c in clusters if c["n_docs"] >= 2)
    print(json.dumps({
        "questions": len(qs), "canon": len(rows_c), "variants": len(rows_v),
        "multi_source_canon": multi_source, "multi_doc_canon": multi_doc,
        "singletons": sum(1 for c in clusters if len(c["members"]) == 1),
        "sticky_aliases": len(aliases),
        "dry_run": args.dry_run,
        "elapsed_sec": round(time.perf_counter() - started, 1),
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题簇落库：canonical_questions + question_variants")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--dist", type=int, default=24, help="SimHash 候选阈值（report 同款）")
    ap.add_argument("--jac", type=float, default=0.7, help="全量 2-gram Jaccard 复核阈值（权威口径）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="只吸附未入 variant 的新题；不重跑全量聚类")
    ap.add_argument("--batch-id", default=None, help="增量批次 ID（默认 inc-时间戳）")
    ap.add_argument("--attach-jac", type=float, default=ATTACH_JAC)
    ap.add_argument("--candidate-jac", type=float, default=CANDIDATE_JAC)
    ap.add_argument("--candidates", action="store_true", help="列出合并候选")
    ap.add_argument("--approve", action="append", metavar="Q_KEY", default=None,
                    help="人工通过候选（可重复）")
    ap.add_argument("--reject", action="append", metavar="Q_KEY", default=None,
                    help="人工驳回候选（可重复）")
    ap.add_argument("--apply-candidates", action="store_true",
                    help="把 approved 候选挂到目标 canonical")
    args = ap.parse_args(argv)
    if (args.dry_run or args.candidates or args.stats) and (
            args.approve or args.reject or args.apply_candidates):
        print(json.dumps({"verdict": "ABORT",
                          "why": "--dry-run/--stats/--candidates 是只读模式，不能同时改候选"},
                         ensure_ascii=False, indent=1), file=sys.stderr)
        return 2
    if args.dry_run or args.stats or args.candidates:
        try:
            return _run(args)
        except FileNotFoundError as ex:
            print(json.dumps({"verdict": "ABORT", "why": str(ex)},
                             ensure_ascii=False, indent=1), file=sys.stderr)
            return 2
    db_path = Path(args.db)
    try:
        with wlock.write_lock("kb", tool="canonical", build_dir=db_path.parent, target=db_path):
            return _run(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另有工具在写 kb.sqlite；canonical 不与之并行"},
                         ensure_ascii=False, indent=1))
        return 2


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
