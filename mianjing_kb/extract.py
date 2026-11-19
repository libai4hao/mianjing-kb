# -*- coding: utf-8 -*-
"""extract：把语料切成题块落 questions 表（P1-1，chunker v2 产出）。

**单身份（P0-1，2026-11-19）**：题池只认一个文本源——有 md 产物的源文档用 md 正文、
身份仍是源 `doc_hash`；无产物的源才直接抽源文件；孤儿 md 保留自身 `md-mirror:` 身份。
映射由 `kb/mdmap.py` 统一裁决（hash8 精确 > 唯一 stem > 歧义裁决 > 孤儿），
报告落 `build/md_identity_report.json`。不再把"源文件 + md 镜像"同时收进题池。

- md/txt 直接读文件；pdf/docx/doc 走 question_freq.extract；<80 字符的空文档记 0 题
- 每题算 q_norm / q_norm_hash / simhash，落 meta/schema/004_questions.sql 的 questions 表
- 频次口径：精确同文直接可用视图 question_freq（见证数 = distinct collection，014 迁移）；
  近重复簇留给 P1-4 report（simhash）
- 状态与产物（Q-3）：开始处理写 `processing`，成功后写 `processed` 并记 `document_products`
  （含 size/mtime，供 `--incremental` 判"没变就跳过"）；失败留 `processing` 作为断点

用法：
    python -m mianjing_kb.extract --limit 20          # 试跑 20 件
    python -m mianjing_kb.extract --stats             # 只看 questions 表分布
    python -m mianjing_kb.extract --plan              # 只看"会抽哪些身份"（不读正文、不写库）
    python -m mianjing_kb.extract --incremental       # 只处理新增/变更文档
    python -m mianjing_kb.extract                     # 全量（幂等：同 doc 覆盖重写）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, mdmap, products, status  # noqa: E402
from mianjing_kb.question_freq import close_word, extract, normalize, simhash  # noqa: E402
from mianjing_kb.chunker import chunk_v2  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
MIN_CHARS = 80
MIN_NORM = 6


def text_of(path: Path) -> str:
    """md/txt 读文件，其余走 question_freq.extract；<80 字符视为无文本。"""
    if path.suffix.lower() in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        text = extract(path)
    if len(text.strip()) < MIN_CHARS:
        return ""
    return re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)


def _excluded_rels() -> tuple[set[str], set[str]]:
    """P2-⑤ 排除表：返回 (源文件 rel 集, md 镜像 rel 集)，正反斜杠归一。"""
    if not config.EXCLUSIONS_FILE.exists():
        return set(), set()
    data = json.loads(config.EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    src = {x.get("rel", "").replace("\\", "/") for x in data.get("items", []) if x.get("rel")}
    mds = {x.get("md_rel", "").replace("\\", "/") for x in data.get("items", []) if x.get("md_rel")}
    return src, mds


def doc_pool(conn) -> tuple[list[tuple[str, Path]], "mdmap.Mapping"]:
    """(doc_hash, path) 题池 —— **一文档一身份**。

    1. 有 md 产物：用 md 正文，身份 = 源 doc_hash（P0-1 单身份）；
    2. 无产物：抽源文件，身份 = 源 doc_hash；
    3. 孤儿 md（找不到源）：保留 `md-mirror:` 身份单独成池。
    exclusions.json 命中的件不进题池（P2-⑤：保留检索、不抽题）；产物被排除时整篇不抽，
    不再回退到源文件（否则排除形同虚设）。
    """
    ex_src, ex_md = _excluded_rels()
    mapping = mdmap.build(conn, Path(config.QUESTION_MD_DIR))
    pool: dict[str, Path] = {}

    for doc_hash, (rel, p) in mapping.by_doc.items():
        if rel in ex_md:
            continue
        pool[doc_hash] = p

    for r in conn.execute(
            f"SELECT doc_hash, src_path FROM source_documents WHERE status IN {status.sql_in(status.INGESTED)}").fetchall():
        doc_hash = r["doc_hash"]
        if doc_hash in pool:             # 已有产物，源文件不再重复抽
            continue
        p = Path(r["src_path"])
        if not p.exists():
            continue
        try:
            rel = p.relative_to(config.CORPUS_ROOT).as_posix()
        except ValueError:
            rel = p.name
        if rel in ex_src:
            continue
        pool[doc_hash] = p

    for mh, (rel, p) in mapping.orphans.items():
        if rel in ex_md:
            continue
        pool.setdefault(mh, p)
    return sorted(pool.items()), mapping


def write_identity_report(mapping) -> Path:
    """`build/md_identity_report.json`：映射率、歧义、多产物、孤儿——单身份的可审计留痕。"""
    build = Path(config.BUILD_DIR)
    build.mkdir(parents=True, exist_ok=True)
    out = build / "md_identity_report.json"
    out.write_text(json.dumps(mapping.report, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _product_loc(conn, doc_hash: str, mapping) -> tuple[str | None, str | None, str]:
    """(md_rel, md_hash, text_source)：产物行已有的 hash 直接复用，不做二次读盘。"""
    prod = products.get(conn, doc_hash)
    if doc_hash in mapping.by_doc:
        return mapping.by_doc[doc_hash][0], prod["md_hash"] if prod else None, "md"
    if doc_hash in mapping.orphans:
        return mapping.orphans[doc_hash][0], prod["md_hash"] if prod else None, "orphan"
    return None, prod["md_hash"] if prod else None, "source"


def mark_status(conn, doc_hashes: list[str], new_status: str, only_from=status.INGESTED) -> int:
    """批量改 source_documents.status（只动内容池里的行；孤儿 md 没有台账行，自然跳过）。"""
    n = 0
    for i in range(0, len(doc_hashes), 500):
        part = doc_hashes[i:i + 500]
        cur = conn.execute(
            f"UPDATE source_documents SET status=? WHERE doc_hash IN ({','.join('?' * len(part))}) "
            f"AND status IN {status.sql_in(only_from)}", [new_status, *part])
        n += cur.rowcount
    return n


def purge_orphans(conn, pool_keys: set[str]) -> int:
    """清理 questions 里 doc_hash 不在当前池的孤儿行（内容 hash 时代遗留/后续源退役）。"""
    stale = [r[0] for r in conn.execute("SELECT DISTINCT doc_hash FROM questions").fetchall()
             if r[0] not in pool_keys]
    removed = 0
    for i in range(0, len(stale), 500):
        chunk = stale[i:i + 500]
        conn.execute(f"DELETE FROM questions WHERE doc_hash IN ({','.join('?' * len(chunk))})", chunk)
        removed += len(chunk)
    return removed


def _signed64(v: int) -> int:
    """simhash 为无符号 64 位，SQLite INTEGER 是有符号——转二补码。"""
    return v - (1 << 64) if v >= (1 << 63) else v


def extract_doc(doc_hash: str, path: Path) -> list[dict]:
    """单文档 → questions 行列表（不落库）。"""
    text = text_of(path)
    if not text:
        return []
    rows = []
    v2 = chunk_v2(text)
    for it in v2["items"]:
        qn = normalize(it["q"])
        if len(qn) < MIN_NORM:
            continue
        rows.append({
            "doc_hash": doc_hash,
            "seq": it["seq"],
            "q_text": it["q"][:2000],
            "a_text": it["a"][:20000],
            "span_start": it["span"][0],
            "span_end": it["span"][1],
            "qtype": it["qtype"],
            "conf": it["conf"],
            "q_norm": qn[:500],
            "q_norm_hash": hashlib.sha256(qn.encode("utf-8")).hexdigest()[:16],
            "simhash": _signed64(simhash(qn)),
            "extracted_at": datetime.now().strftime(NOW_FMT),
        })
    return rows


def store(conn, doc_hash: str, rows: list[dict]) -> int:
    """幂等写入：先删该 doc 旧题再插（文档内容变了整篇重切）。

    B1 修复（2026-11-19）：插入后按 q_norm_hash 重挂 label_facts（007）里已 merge
    的打标——重切会换 qid（AUTOINCREMENT 高水位续号），打标事实按题干 hash 存活。
    返回重挂数。
    """
    conn.execute("DELETE FROM questions WHERE doc_hash=?", (doc_hash,))
    if rows:
        cols = ",".join(rows[0].keys())
        ph = ",".join("?" for _ in rows[0])
        conn.executemany(
            f"INSERT INTO questions ({cols}) VALUES ({ph})",
            [[r[c] for c in rows[0]] for r in rows],
        )
    cur = conn.execute(
        "UPDATE questions SET "
        " topic=(SELECT lf.topic FROM label_facts lf WHERE lf.q_norm_hash=questions.q_norm_hash), "
        " qtype=COALESCE((SELECT lf.qtype FROM label_facts lf "
        "                WHERE lf.q_norm_hash=questions.q_norm_hash), qtype), "
        " difficulty=(SELECT lf.difficulty FROM label_facts lf "
        "             WHERE lf.q_norm_hash=questions.q_norm_hash), "
        " tags=(SELECT lf.tags FROM label_facts lf WHERE lf.q_norm_hash=questions.q_norm_hash) "
        "WHERE doc_hash=? AND q_norm_hash IN (SELECT q_norm_hash FROM label_facts)", (doc_hash,))
    return cur.rowcount


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P1-1：题块落 questions 表")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true", help="只看 questions 表分布")
    ap.add_argument("--plan", action="store_true",
                    help="只看身份映射与题池规模（不读正文、不写库）")
    ap.add_argument("--incremental", action="store_true",
                    help="只处理新增/变更文档（按 document_products 的 size/mtime 判定）")
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=not (args.dry_run or args.plan))
    if args.stats:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        out = {"questions": n,
               "by_doc": conn.execute("SELECT COUNT(DISTINCT doc_hash) FROM questions").fetchone()[0],
               "qtype": dict(conn.execute("SELECT qtype, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
               "conf": dict(conn.execute("SELECT conf, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
               "freq_top10": [dict(r) for r in conn.execute(
                   "SELECT sample_q, n_distinct_docs FROM question_freq LIMIT 10").fetchall()]}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    pool, mapping = doc_pool(conn)
    report_path = write_identity_report(mapping)
    if args.plan:
        print(json.dumps({
            "pool_docs": len(pool),
            "md_mapped_sources": len(mapping.by_doc),
            "source_only": len(pool) - len(mapping.by_doc) - len(mapping.orphans),
            "orphan_md": len(mapping.orphans),
            "mapping": {k: v for k, v in mapping.report.items()
                        if k not in ("mapping", "conflicts", "duplicates", "orphans_list")},
            "report": str(report_path),
        }, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    # 产物台账先落"映射事实"（extracted_at 留空），增量判据才有输入
    if not args.dry_run:
        products.sync_mapping(conn, mapping)
        conn.commit()
    pool_total = len(pool)
    if args.incremental:
        pool = [x for x in pool if products.needs_work(conn, x[0], x[1])]
    skipped_unchanged = pool_total - len(pool)
    if args.limit:
        pool = pool[:args.limit]
    started = time.perf_counter()
    if not args.dry_run and pool:
        mark_status(conn, [h for h, _p in pool], status.PROCESSING)
        conn.commit()
    n_docs = n_q = n_empty = n_err = n_reattached = 0
    qtype: dict[str, int] = {}
    conf: dict[str, int] = {}
    for i, (doc_hash, p) in enumerate(pool):
        try:
            rows = extract_doc(doc_hash, p)
        except Exception as ex:  # noqa: BLE001
            n_err += 1
            # 失败留 processing：状态本身就是"下次从这续跑"的标记
            print(f"[warn] {p.name}: {type(ex).__name__}: {ex}", file=sys.stderr)
            continue
        n_docs += 1
        if not rows:
            n_empty += 1
        n_q += len(rows)
        for r in rows:
            qtype[r["qtype"]] = qtype.get(r["qtype"], 0) + 1
            conf[r["conf"]] = conf.get(r["conf"], 0) + 1
        if not args.dry_run:
            n_reattached += store(conn, doc_hash, rows)
            md_rel, md_hash, text_source = _product_loc(conn, doc_hash, mapping)
            products.upsert(conn, doc_hash, md_rel=md_rel, md_hash=md_hash,
                            text_source=text_source, input_path=p, questions_n=len(rows),
                            extracted_at=datetime.now().strftime(NOW_FMT))
            mark_status(conn, [doc_hash], status.PROCESSED)
            if (i + 1) % 50 == 0:            # 增量/长批次的分块提交（崩溃只回退当前块）
                conn.commit()
        if (i + 1) % 200 == 0:
            print(f"[prog] {i + 1}/{len(pool)} docs, {n_q} questions", file=sys.stderr)
    close_word()        # Word COM 只在抽取阶段用；批次结束立刻释放，不带着宿主进程进落库/统计
    if not args.dry_run:
        # 全量才清孤儿；增量只处理子集，purge 会把未在本次池里的题行误删
        n_purged = 0 if args.incremental else purge_orphans(conn, {k for k, _ in pool})
        n_lost = conn.execute("SELECT COUNT(*) FROM label_facts WHERE q_norm_hash NOT IN "
                              "(SELECT q_norm_hash FROM questions)").fetchone()[0]
        if n_lost:
            print(f"[warn] label_facts 有 {n_lost} 条打标已无对应题（重切后真正丢失），"
                  f"详见 docs/项目审核报告/修复方案-2026-11-19.md F1", file=sys.stderr)
        conn.commit()
    else:
        n_lost = 0
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "docs": n_docs, "questions": n_q, "empty_docs": n_empty, "errors": n_err,
        "incremental": args.incremental, "skipped_unchanged": skipped_unchanged,
        "purged_orphan_docs": n_purged if not args.dry_run else 0,
        "label_reattached": n_reattached if not args.dry_run else 0,
        "label_facts_lost": n_lost,
        "dry_run": args.dry_run, "elapsed_sec": round(elapsed, 1),
        "identity": {"md_mapped_sources": len(mapping.by_doc),
                     "orphan_md": len(mapping.orphans),
                     "ambiguous": mapping.report.get("ambiguous", 0),
                     "multi_md_docs": mapping.report.get("docs_multi_md", 0),
                     "report": str(report_path)},
        "products": products.summary(conn) if not args.dry_run else {},
        "qtype": qtype, "conf": conf,
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
