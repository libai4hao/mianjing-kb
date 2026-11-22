# -*- coding: utf-8 -*-
"""空答案批量补答（迁移 019 配套）：本地模型逐题作答，写回 `questions.a_text` 并留痕。

口径（docs/49）：
- 目标 = `TRIM(a_text)=''` 且 `answer_source=''`，默认排除 `toc_noise`（目录伪题）；
- 模型输出 `NO_ANSWER` → 不写正文，记 `answer_source='llm_refused'`，下轮不再重试；
- 写回带 `answer_source='llm'`、`answer_model`、`answered_at`，可用一条 SQL 全量回滚；
- 可断点续跑：已 `answer_source != ''` 的题自动跳过。

    python -m mianjing_kb.answer_backfill --stats
    python -m mianjing_kb.answer_backfill --limit 300 --concurrency 4 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import llm_client  # noqa: E402
from mianjing_kb.kb import config, db  # noqa: E402

SYSTEM = (
    "你是 Java/后端面试题答题助手。只输出答案正文，不要复述题目、不要加编号、不要客套。"
    "基于通用知识作答，控制在 150 字以内；若题目信息不足以确定要问什么、或明显不是一道题，"
    "只输出一行：NO_ANSWER"
)
MIN_Q_CHARS = 6
REFUSED = "llm_refused"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def pick_targets(conn, limit: int = 0, states: tuple[str, ...] | None = None) -> list[tuple]:
    """待补答：空答案、未留痕、非目录伪题。"""
    states = states or ("boundary_gap", "no_answer_source", "ok")
    marks = ",".join("'%s'" % s for s in states)
    sql = ("SELECT qid, q_text FROM questions "
           "WHERE TRIM(COALESCE(a_text,''))='' AND COALESCE(answer_source,'')='' "
           f"AND COALESCE(answer_state,'') IN ({marks}) "
           "ORDER BY answer_state, qid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def stats(conn) -> dict:
    rows = conn.execute(
        "SELECT COALESCE(answer_source,'') AS src, COUNT(*) FROM questions "
        "WHERE TRIM(COALESCE(a_text,''))='' GROUP BY src").fetchall()
    by = {k or "(原文空)": v for k, v in rows}
    left = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE TRIM(COALESCE(a_text,''))='' "
        "AND COALESCE(answer_source,'')='' AND COALESCE(answer_state,'') "
        "IN ('boundary_gap','no_answer_source','ok')").fetchone()[0]
    return {"empty_by_source": by, "todo": left}


def _ask(cfg, q_text: str) -> tuple[str, str]:
    """返回 (答案文本, source)；拒答/失败给空文本 + 对应 source。"""
    try:
        r = llm_client.chat(cfg, SYSTEM, q_text)
        text = (r.get("text") or "").strip()
    except Exception:  # noqa: BLE001 —— 单条失败不拖垮整批
        return "", "llm_error"
    if not text:
        return "", "llm_error"
    if text.upper().startswith("NO_ANSWER"):
        return "", REFUSED
    return text, "llm"


def run(conn, qids: list[tuple], *, concurrency: int = 2, dry_run: bool = False,
        out_dir: Path | None = None) -> dict:
    cfg = llm_client.resolve_config()
    todo = [(q, t) for q, t in qids if len((t or "").strip()) >= MIN_Q_CHARS]
    skipped_short = len(qids) - len(todo)
    stats_out = {"todo": len(qids), "skipped_short": skipped_short, "answered": 0,
                 "refused": 0, "error": 0, "elapsed_sec": 0, "model": cfg.get("model")}
    t0 = time.time()
    log = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {q: pool.submit(_ask, cfg, t) for q, t in todo}
        for i, (q, _t) in enumerate(todo, 1):
            text, src = futures[q].result()
            log.append({"qid": q, "source": src, "chars": len(text)})
            if src == "llm":
                stats_out["answered"] += 1
            elif src == REFUSED:
                stats_out["refused"] += 1
            else:
                stats_out["error"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE questions SET a_text=?, answer_source=?, answer_model=?, answered_at=? "
                    "WHERE qid=?", (text, src, cfg.get("model"), _now(), q))
                if i % 50 == 0:
                    conn.commit()
            if i % 200 == 0:
                print(json.dumps({"done": i, "of": len(todo), **{k: stats_out[k] for k in
                                  ("answered", "refused", "error")}}, ensure_ascii=False))
    if not dry_run:
        conn.commit()
    stats_out["elapsed_sec"] = round(time.time() - t0, 1)
    stats_out["per_q_sec"] = round(stats_out["elapsed_sec"] / max(1, len(todo)), 2)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "backfill-log.jsonl", "w", encoding="utf-8", newline="\n") as fh:
            for r in log:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return stats_out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="空答案批量补答（本地模型，留痕可回滚）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--limit", type=int, default=0, help="最多补 N 条（0=全部）")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--apply", action="store_true", help="写库（默认只跑不写）")
    ap.add_argument("--stats", action="store_true", help="只看还剩多少、分布如何")
    ap.add_argument("--state", action="append", default=None,
                    help="只补这些 answer_state（默认 boundary_gap/no_answer_source/ok）")
    ap.add_argument("--out", default="", help="日志目录（默认 <build>/answer-backfill）")
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=True)          # 顺带跑 019 迁移
    try:
        if args.stats:
            print(json.dumps(stats(conn), ensure_ascii=False))
            return 0
        targets = pick_targets(conn, args.limit, tuple(args.state) if args.state else None)
        print(json.dumps({"picked": len(targets), "apply": args.apply}, ensure_ascii=False))
        if not targets:
            return 0
        out = Path(args.out or (Path(args.db).parent / "answer-backfill"))
        res = run(conn, targets, concurrency=args.concurrency, dry_run=not args.apply,
                  out_dir=out)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
