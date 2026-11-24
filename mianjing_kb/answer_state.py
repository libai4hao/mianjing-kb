# -*- coding: utf-8 -*-
"""answer_state：给每道题标「答案从哪来」，让抽检不再把"源文档没答案"误判成切块缺陷。

值域（018 迁移加的 questions.answer_state）：

- `ok`               原文有答案（a_text 非空）；
- `no_answer_source` 源文档本身只列问题（面经回忆、题库清单、PDF 目录被当正文），
                     不是切块 bug，抽检的"答案缺失"缺陷率不该算它；
- `boundary_gap`     同文档多数题有答案、这一条却空 —— 疑似切块错位/答案被邻题吃掉，要修；
- `toc_noise`        目录区/清单区整段推入的伪题（连续空答案 + 标题式占比高），候选退役。

判定口径（确定性、只看结构，不猜语义）：

1. 文档级答案覆盖率 = 该 doc 非空答案题 / 该 doc 总题。
2. 覆盖率 ≤ `--doc-coverage-min`（默认 0.5）→ 该文档的空答案题记 `no_answer_source`：
   一篇里过半的题都没答案，说明来源本身是"问题清单"，不是切块切坏了某一题。
3. 覆盖率高于阈值 → 空答案题记 `boundary_gap`。
4. 命中 `qsalvage.toc_runs()` 的连续空答案段（≥8 块、标题式 ≥80%）→ 记 `toc_noise`
   （优先级最高，其所属文档的整体归类仍可并存）。

用法：
    python -m mianjing_kb.answer_state --db <kb.sqlite>           # 预览分布（只读打开，不迁移不改库）
    python -m mianjing_kb.answer_state --db <kb.sqlite> --only-missing --apply   # 只补空状态并写库
    python -m mianjing_kb.answer_state --db <kb.sqlite> --apply   # 全库重算并写库（会改写历史状态，慎用）

`extract` 重切过的文档，题行上的 answer_state 会被清空——重切后用 `--only-missing --apply` 补回。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, wlock  # noqa: E402
from mianjing_kb.qsalvage import toc_runs  # noqa: E402

OK = "ok"
NO_SOURCE = "no_answer_source"
GAP = "boundary_gap"
TOC = "toc_noise"
STATES = (OK, NO_SOURCE, GAP, TOC)


def classify(conn, doc_coverage_min: float = 0.5, run_min_len: int = 8) -> dict:
    """算出 qid → state 映射与统计。只读。"""
    from collections import defaultdict as _dd

    rows = conn.execute(
        "select qid, doc_hash, seq, coalesce(a_text,''), q_text from questions "
        "order by doc_hash, seq"
    ).fetchall()

    per_doc: dict[str, list] = _dd(list)
    for qid, dh, seq, a, q in rows:
        per_doc[dh].append((qid, seq, a, q or ""))

    doc_cov = {}
    state: dict[int, str] = {}
    for dh, items in per_doc.items():
        answered = sum(1 for x in items if x[2].strip())
        doc_cov[dh] = answered / len(items) if items else 0.0

    for qid, dh, seq, a, q in rows:
        state[qid] = OK if a.strip() else (
            NO_SOURCE if doc_cov[dh] <= doc_coverage_min else GAP)

    for dh, items in per_doc.items():
        runs = toc_runs([(x[1], x[3], x[2]) for x in items], min_len=run_min_len)
        for run in runs:
            seqs = {r[0] for r in run}
            for qid, seq, a, q in items:
                if seq in seqs and not a.strip():
                    state[qid] = TOC

    counts = Counter(state.values())
    empty_docs = sum(1 for dh, items in per_doc.items()
                    if any(not x[2].strip() for x in items))
    return {
        "state": state,
        "counts": dict(counts),
        "total": len(rows),
        "empty": len(rows) - counts.get(OK, 0),
        "doc_coverage_empty_docs": empty_docs,
        "doc_coverage_median": sorted(doc_cov.values())[len(doc_cov) // 2] if doc_cov else 0,
        "doc_coverage_min": doc_coverage_min,
    }


def backfill(conn, apply_result: dict, dry: bool = False) -> dict:
    """把归类写进 questions.answer_state；幂等。"""
    if dry:
        return {"written": 0, "dry": True}
    n = 0
    for qid, st in apply_result["state"].items():
        n += conn.execute(
            "update questions set answer_state=? where qid=? and coalesce(answer_state,'')<>?",
            (st, qid, st),
        ).rowcount
    return {"written": n, "dry": False}


def summary(conn) -> dict:
    """读当前 answer_state 分布（未回填行记 missing）。"""
    counts = {r[0] or "missing": r[1] for r in conn.execute(
        "select answer_state, count(*) from questions group by 1")}
    empty = conn.execute(
        "select count(*) from questions where a_text is null or trim(a_text)=''").fetchone()[0]
    return {"counts": counts, "empty": empty}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="空答案成因标记（ok / 源文档缺答案 / 切块缺口 / 目录噪声）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--apply", action="store_true", help="写库（默认只预览）")
    ap.add_argument("--doc-coverage-min", type=float, default=0.5)
    ap.add_argument("--run-min-len", type=int, default=8)
    ap.add_argument("--only-missing", action="store_true",
                    help="只给 answer_state 为空的行补判；已有状态一律不动"
                         "（生产状态含历史治理/人工修正，全库重算会改写数千行）")
    args = ap.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(json.dumps({"error": f"数据库不存在：{path}"}, ensure_ascii=False))
        return 2
    apply = bool(args.apply)
    # 预览一律以 mode=ro 打开：不迁移、不写 WAL、不抢写锁。旧实现预览也走 db.migrate()，
    # 对回滚快照/旧备份"看一眼分布"就会把它升到最新 schema（评审 P1-1，2026-11-25）。
    # 写库则先拿写锁再连接：迁移（db.connect 内）因此落在锁内，不会与别的写库工具撞车。
    lock = nullcontext()
    if apply:
        lock = wlock.write_lock(
            "kb" if path.resolve() == Path(config.DB_PATH).resolve() else "online",
            tool="answer_state", build_dir=config.BUILD_DIR, wait_sec=30.0, target=path)
    try:
        with lock:
            conn = db.connect(path, create=False, readonly=not apply)
            try:
                if apply:
                    db.migrate(conn)          # 在写锁内迁移（create=False 不会自动迁移）
                res = dict(classify(conn, doc_coverage_min=args.doc_coverage_min,
                                    run_min_len=args.run_min_len))
                if args.only_missing:
                    have = {r[0] for r in conn.execute(
                        "select qid from questions where coalesce(answer_state,'')<>''")}
                    res["state"] = {q: s for q, s in res["state"].items() if q not in have}
                    res["counts"] = dict(Counter(res["state"].values()))
                    res["total"] = len(res["state"])
                    res["empty"] = len(res["state"]) - res["counts"].get(OK, 0)
                out = {"total": res["total"], "empty": res["empty"], "counts": res["counts"],
                       "doc_coverage_median": round(res["doc_coverage_median"], 3),
                       "doc_coverage_min": res["doc_coverage_min"],
                       "docs_with_empty": res["doc_coverage_empty_docs"], "apply": apply,
                       "only_missing": bool(args.only_missing)}
                if apply:
                    with conn:
                        out.update(backfill(conn, res))
                    out["after"] = summary(conn)["counts"]
                print(json.dumps(out, ensure_ascii=False, indent=1))
            finally:
                conn.close()
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另有工具在写这个库；answer_state 不与之并行，稍后重试"},
                         ensure_ascii=False, indent=1))
        return 2
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
