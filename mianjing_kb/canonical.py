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
from mianjing_kb.kb import config, db  # noqa: E402
from mianjing_kb.report import build_clusters, load_questions  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


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
                           meta.get(q["doc_hash"], ("", ""))[1], now))
    return rows_c, rows_v


def write_all(conn, rows_c: list[tuple], rows_v: list[tuple]) -> None:
    conn.execute("DELETE FROM question_variants")
    conn.execute("DELETE FROM canonical_questions")
    conn.executemany(
        "INSERT INTO canonical_questions "
        "(canonical_id, title_norm, topic, center_vec, witness, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)", rows_c)
    conn.executemany(
        "INSERT INTO question_variants "
        "(variant_id, canonical_id, doc_hash, question_no, answering_text, collection, created_at) "
        "VALUES (?,?,?,?,?,?,?)", rows_v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题簇落库：canonical_questions + question_variants")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--dist", type=int, default=24, help="SimHash 候选阈值（report 同款）")
    ap.add_argument("--jac", type=float, default=0.7, help="2-gram Jaccard 复核阈值")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=not args.dry_run)
    if args.stats:
        out = {
            "canon": conn.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0],
            "variants": conn.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0],
            "multi_doc": conn.execute(
                "SELECT COUNT(*) FROM canonical_questions WHERE witness >= 2").fetchone()[0],
            "topic_top10": dict(conn.execute(
                "SELECT COALESCE(topic, '<null>') AS t, COUNT(*) AS n FROM canonical_questions "
                "GROUP BY 1 ORDER BY n DESC LIMIT 10").fetchall()),
        }
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
    multi = sum(1 for c in clusters if c["n_docs"] >= 2)
    print(json.dumps({
        "questions": len(qs), "canon": len(rows_c), "variants": len(rows_v),
        "multi_doc_canon": multi, "singletons": len(rows_c) - multi,
        "dry_run": args.dry_run,
        "elapsed_sec": round(time.perf_counter() - started, 1),
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
