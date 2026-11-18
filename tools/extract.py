# -*- coding: utf-8 -*-
"""extract：把语料切成题块落 questions 表（P1-1，chunker v2 产出）。

- 文档池 = source_documents 里 status='classified' 的件 + 20-题库MD 的 md 镜像（与 cluster 同池）
- md/txt 直接读文件；pdf/docx/doc 走 question_freq.extract；<80 字符的空文档记 0 题
- 每题算 q_norm / q_norm_hash / simhash，落 meta/schema/004_questions.sql 的 questions 表
- 频次口径：精确同文直接可用视图 question_freq；近重复簇留给 P1-4 report（simhash）

用法：
    python tools/extract.py --limit 20          # 试跑 20 件
    python tools/extract.py --stats             # 只看 questions 表分布
    python tools/extract.py                     # 全量（幂等：同 doc 覆盖重写）
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
from kb import config, db, status  # noqa: E402
from question_freq import close_word, extract, normalize, simhash  # noqa: E402
from chunker import chunk_v2  # noqa: E402

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


def _md_mirror_paths() -> tuple[list[Path], int]:
    """返回 (参与抽题的 md, 被跳过的旧同名 md 数)。

    reconvert/backfill 生成的 `x_<hash8>.md` 与旧 `x.md` 会在 rglob 里同时出现；
    同一目录同一 base 已有带哈希版本时跳过旧文件，避免同一原文抽两遍、题频虚高。
    """
    import re
    paths = sorted(config.QUESTION_MD_DIR.rglob("*.md")) if config.QUESTION_MD_DIR.exists() else []
    pat = re.compile(r"(?:__|_)[0-9a-f]{8}$")
    hashed = {(p.parent, pat.sub("", p.stem)) for p in paths if pat.search(p.stem)}
    kept, skipped = [], 0
    for p in paths:
        if not pat.search(p.stem) and (p.parent, p.stem) in hashed:
            skipped += 1
            continue
        kept.append(p)
    return kept, skipped


def doc_pool(conn) -> list[tuple[str, Path]]:
    """(doc_hash, path)：classified 件 + 20-题库MD md 镜像（无行孤儿按路径自算 hash 前缀）。
    exclusions.json 命中的件不进题池（P2-⑤：保留检索、不抽题）。"""
    pool: dict[str, Path] = {}
    ex_src, ex_md = _excluded_rels()
    for r in conn.execute(
            f"SELECT doc_hash, src_path FROM source_documents WHERE status='{status.CLASSIFIED}'").fetchall():
        p = Path(r["src_path"])
        if not p.exists():
            continue
        try:
            rel = p.relative_to(config.CORPUS_ROOT).as_posix()
        except ValueError:
            rel = p.name
        if rel in ex_src:
            continue
        pool[r["doc_hash"]] = p
    if config.QUESTION_MD_DIR.exists():
        paths, _skipped = _md_mirror_paths()
        for p in paths:
            # 镜像 md 无 DB 行：doc_hash 按路径稳定（内容 hash 会在 md 被改写时产生孤儿行——
            # OCR 合并/懒加载每改一次 md，旧 hash 的题行就残留累积，2026-11-17 修复）
            rel = p.relative_to(config.QUESTION_MD_DIR).as_posix()
            if rel in ex_md:
                continue
            h = hashlib.sha256(("md-mirror:" + rel).encode("utf-8")).hexdigest()[:16]
            pool.setdefault(h, p)
    return sorted(pool.items())


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
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=not args.dry_run)
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

    pool = doc_pool(conn)
    if args.limit:
        pool = pool[:args.limit]
    started = time.perf_counter()
    n_docs = n_q = n_empty = n_err = n_reattached = 0
    qtype: dict[str, int] = {}
    conf: dict[str, int] = {}
    for i, (doc_hash, p) in enumerate(pool):
        try:
            rows = extract_doc(doc_hash, p)
        except Exception as ex:  # noqa: BLE001
            n_err += 1
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
        if (i + 1) % 200 == 0:
            print(f"[prog] {i + 1}/{len(pool)} docs, {n_q} questions", file=sys.stderr)
    close_word()        # Word COM 只在抽取阶段用；批次结束立刻释放，不带着宿主进程进落库/统计
    if not args.dry_run:
        n_purged = purge_orphans(conn, {k for k, _ in pool})
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
        "purged_orphan_docs": n_purged if not args.dry_run else 0,
        "label_reattached": n_reattached if not args.dry_run else 0,
        "label_facts_lost": n_lost,
        "dry_run": args.dry_run, "elapsed_sec": round(elapsed, 1),
        "qtype": qtype, "conf": conf,
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
