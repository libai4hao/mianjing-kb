# -*- coding: utf-8 -*-
"""canonical：questions 表 → 题簇落库（canonical_questions / question_variants 全量重建）。

「题级吸附」的存储层（docs/03 §10.2 canonical 表，2026-11-17 落地）：
  - canonical_questions  一行 = 一个"同一道题"（近重复簇），witness = 见证来源数
                          （distinct collection；collection 为空回退 doc_hash，Q-2 2026-11-19）
  - question_variants    一行 = 该题的一个变体（源 doc_hash + seq），FK 级联
聚类复用 report.build_clusters（simhash LSH 分带 + 汉明 + 2-gram Jaccard 双确认，P1-4 同款）；
单件簇也落库——canonical 是全量唯一题登记簿，不只存重复簇。

已知简化（ponytail: ceiling 与升级路径）：
  - 全量重建而非增量吸附：13.5k 题全量聚类秒级，增量匹配的复杂度在当前量级不划算；
    题量到 1e5 或需要流式接入时再改增量（question_freq.DSU 现成）。
  - center_vec 留 NULL：向量层接入后回填（bge 向量的簇心，供语义近邻召回）。

用法：
    python -m mianjing_kb.canonical            # 全量重建（先清两表再写，幂等）
    python -m mianjing_kb.canonical --stats    # 只看落库分布
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import manifests  # noqa: E402
from mianjing_kb.kb import config, db, wlock  # noqa: E402
from mianjing_kb.report import build_clusters, load_questions  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
ATTACH_JAC = 0.92       # T29：≥0.92 直接吸附
CANDIDATE_JAC = 0.85    # 0.85–0.92 只登记候选，等待人工确认


def variant_id(doc_hash: str, seq: int) -> str:
    """变体主键：sha256(doc_hash:seq)[:16]——extract 重跑 qid 会漂移，(doc_hash, seq) 才稳定。"""
    return hashlib.sha256(f"{doc_hash}:{seq}".encode("utf-8")).hexdigest()[:16]


def doc_meta(conn) -> dict[str, tuple[str, str]]:
    """{doc_hash: (topic, collection)}——variant 的 collection 归属。"""
    return {r["doc_hash"]: (r["topic"] or "", r["collection"] or "")
            for r in conn.execute("SELECT doc_hash, topic, collection FROM source_documents")}


def build_rows(qs: list[dict], clusters: list[dict],
               meta: dict[str, tuple[str, str]], now: str) -> tuple[list[tuple], list[tuple]]:
    """簇 → 两表行。canonical_id = 代表题干（最多变体里最长）norm 的 sha256[:16]，
    稳定且不同簇必不同（同 norm 必同簇：simhash 相同同桶强并）。topic = 成员文档 topic
    的多数票（空/未分类不入票）。"""
    rows_c: list[tuple] = []
    rows_v: list[tuple] = []
    for c in clusters:
        norms = Counter(qs[i]["q_norm"] for i in c["members"])
        best = max(norms, key=lambda n: (norms[n], len(n)))
        cid = hashlib.sha256(best.encode("utf-8")).hexdigest()[:16]
        topics = Counter()
        for i in c["members"]:
            t = qs[i].get("topic") or ""
            if t and t != config.UNCLASSIFIED:
                topics[t] += 1
        topic = topics.most_common(1)[0][0] if topics else None
        # Q-2（2026-11-19）：见证数 = distinct collection；collection 为空回退到 doc_hash
        # （不能把未知来源全算成 1 个，否则少报；也不能按 doc_hash 计，否则同一门课虚高）
        witness = len({meta.get(qs[i]["doc_hash"], ("", ""))[1]
                       or "doc:" + qs[i]["doc_hash"] for i in c["members"]})
        rows_c.append((cid, best[:500], topic, None, witness, now, now))
        for i in c["members"]:
            q = qs[i]
            rows_v.append((variant_id(q["doc_hash"], q["seq"]), cid, q["doc_hash"],
                           str(q["seq"]), (q.get("a_text") or "")[:2000],
                           meta.get(q["doc_hash"], ("", ""))[1], now,
                           q.get("q_key") or f"{q['doc_hash']}:{q['q_norm_hash']}"))
    return rows_c, rows_v


def write_all(conn, rows_c: list[tuple], rows_v: list[tuple]) -> None:
    conn.execute("DELETE FROM question_variants")
    conn.execute("DELETE FROM canonical_questions")
    conn.execute("DELETE FROM canonical_dependents")
    conn.execute("DELETE FROM merge_candidates")
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


def _grams(text: str) -> set[str]:
    s = " ".join((text or "").split()).lower()
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def similarity(a: str, b: str) -> float:
    """字符 2-gram Jaccard；相同题干=1，空串=0。"""
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _recompute_witness(conn, canonical_id: str) -> int:
    n = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(collection),''), 'doc:'||doc_hash)) "
        "FROM canonical_dependents WHERE canonical_id=?", (canonical_id,)).fetchone()[0]
    conn.execute("UPDATE canonical_questions SET witness=?, updated_at=? WHERE canonical_id=?",
                 (n, datetime.now().strftime(NOW_FMT), canonical_id))
    return int(n)


def attach_incremental(conn, attach_jac: float = ATTACH_JAC,
                       candidate_jac: float = CANDIDATE_JAC,
                       batch_id: str | None = None) -> dict:
    """T29：只处理尚未进 variant 的新题，吸附/候选/新建三路，不重跑全量聚类。

    - q_key 已在 question_variants 的题跳过（幂等）；
    - Jaccard ≥ attach_jac → 吸到最相似 canonical；
    - candidate_jac ≤ sim < attach_jac → merge_candidates(status=pending)，不落 variant；
    - 其余 → 新 canonical + variant；
    - 只重算 touched canonical 的 witness（来自 canonical_dependents）。
    """
    now = datetime.now().strftime(NOW_FMT)
    attached_keys = {r[0] for r in conn.execute(
        "SELECT q_key FROM question_variants WHERE COALESCE(q_key,'')!=''")}
    reps = [(r[0], r[1]) for r in conn.execute(
        "SELECT canonical_id, title_norm FROM canonical_questions")]
    by_norm = {t: cid for cid, t in reps}
    meta = doc_meta(conn)
    created: list[str] = []
    attached: list[str] = []
    added_variants: list[str] = []
    candidates: list[dict] = []
    touched: set[str] = set()

    for q in load_questions(conn):
        qkey = q.get("q_key") or f"{q['doc_hash']}:{q['q_norm_hash']}"
        if qkey in attached_keys:
            continue
        norm = q.get("q_norm") or ""
        best_cid, best_sim = None, 0.0
        if norm in by_norm:
            best_cid, best_sim = by_norm[norm], 1.0
        else:
            for cid, title in reps:
                s = similarity(norm, title)
                if s > best_sim:
                    best_cid, best_sim = cid, s
        if best_cid and best_sim < attach_jac and best_sim >= candidate_jac:
            conn.execute(
                "INSERT INTO merge_candidates(q_key,canonical_id,similarity,status,created_at,updated_at) "
                "VALUES(?,?,?, 'pending', ?,?) ON CONFLICT(q_key) DO UPDATE SET "
                "canonical_id=excluded.canonical_id, similarity=excluded.similarity, "
                "status='pending', updated_at=excluded.updated_at",
                (qkey, best_cid, round(best_sim, 4), now, now))
            candidates.append({"q_key": qkey, "canonical_id": best_cid,
                               "similarity": round(best_sim, 4)})
            continue
        if not best_cid or best_sim < attach_jac:
            cid = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
            if cid not in by_norm.values() and conn.execute(
                    "SELECT COUNT(*) FROM canonical_questions WHERE canonical_id=?",
                    (cid,)).fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO canonical_questions"
                    "(canonical_id,title_norm,topic,center_vec,witness,created_at,updated_at) "
                    "VALUES(?,?,?,NULL,0,?,?)",
                    (cid, norm[:500], q.get("topic") or None, now, now))
                created.append(cid)
            best_cid, best_sim = cid, 1.0
            by_norm[norm] = cid
            reps.append((cid, norm))
        cid = best_cid
        collection = meta.get(q["doc_hash"], ("", ""))[1]
        conn.execute(
            "INSERT OR IGNORE INTO question_variants"
            "(variant_id,canonical_id,doc_hash,question_no,answering_text,collection,created_at,q_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (variant_id(q["doc_hash"], q["seq"]), cid, q["doc_hash"], str(q["seq"]),
             (q.get("a_text") or "")[:2000], collection, now, qkey))
        conn.execute(
            "INSERT OR IGNORE INTO canonical_dependents(canonical_id,q_key,doc_hash,collection) "
            "VALUES(?,?,?,?)", (cid, qkey, q["doc_hash"], collection))
        if cid not in created:
            attached.append(qkey)
        added_variants.append(qkey)
        touched.add(cid)
        attached_keys.add(qkey)

    for cid in touched:
        _recompute_witness(conn, cid)
    payload = {"tool": "canonical-incremental",
               "created_canonicals": created, "attached": attached,
               "candidates": candidates, "added_variants": added_variants,
               "touched_canonicals": sorted(touched)}
    if batch_id:
        payload["manifest"] = str(manifests.create(batch_id, payload).get("batch_id"))
    return {"new_questions": len(added_variants) + len(candidates),
            "created_canonicals": len(created), "attached": len(attached),
            "candidates": len(candidates), "added_variants": len(added_variants),
            "batch_id": batch_id, **payload}


def _run(args) -> int:
    conn = db.connect(args.db, create=not args.dry_run)
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
        if args.dry_run:
            conn.execute("SAVEPOINT inc")
            out = attach_incremental(conn, args.attach_jac, args.candidate_jac, None)
            conn.execute("ROLLBACK TO inc")
            conn.execute("RELEASE inc")
        else:
            out = attach_incremental(conn, args.attach_jac, args.candidate_jac, batch_id)
            conn.commit()
        out["dry_run"] = bool(args.dry_run)
        out["elapsed_sec"] = round(time.perf_counter() - started, 1)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    started = time.perf_counter()
    qs = load_questions(conn)
    clusters = build_clusters(qs, args.dist, args.jac)
    rows_c, rows_v = build_rows(qs, clusters, doc_meta(conn), datetime.now().strftime(NOW_FMT))
    if not args.dry_run:
        write_all(conn, rows_c, rows_v)
        conn.commit()
    multi_source = sum(1 for c in clusters if c["n_collections"] >= 2)
    multi_doc = sum(1 for c in clusters if c["n_docs"] >= 2)
    print(json.dumps({
        "questions": len(qs), "canon": len(rows_c), "variants": len(rows_v),
        "multi_source_canon": multi_source, "multi_doc_canon": multi_doc,
        "singletons": len(rows_c) - multi_source,
        "dry_run": args.dry_run,
        "elapsed_sec": round(time.perf_counter() - started, 1),
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题簇落库：canonical_questions + question_variants")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--dist", type=int, default=24, help="SimHash 候选阈值（report 同款）")
    ap.add_argument("--jac", type=float, default=0.7, help="2-gram Jaccard 复核阈值")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="只吸附未入 variant 的新题；不重跑全量聚类")
    ap.add_argument("--batch-id", default=None, help="增量批次 ID（默认 inc-时间戳）")
    ap.add_argument("--attach-jac", type=float, default=ATTACH_JAC)
    ap.add_argument("--candidate-jac", type=float, default=CANDIDATE_JAC)
    args = ap.parse_args(argv)
    if args.dry_run or args.stats:
        return _run(args)
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
