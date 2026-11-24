# -*- coding: utf-8 -*-
"""空答案批量补答（迁移 019 配套）：本地模型逐题作答，写回 `questions.a_text` 并留痕。

口径（docs/49）：
- 目标 = `TRIM(a_text)=''` 且 `answer_source=''`，默认排除 `toc_noise`（目录伪题）；
- 模型输出 `NO_ANSWER` → 不写正文，记 `answer_source='llm_refused'`，下轮不再重试；
- 写回带 `answer_source='llm'`、`answer_model`、`answered_at`，可用一条 SQL 全量回滚；
- 可断点续跑：已 `answer_source != ''` 的题自动跳过。

写库口径（评审 P1-2，2026-11-25）：
- 不写库的预览以只读连接打开，且默认最多调 20 条模型（`--limit 0` 才全量预览）；
- 写库按 50 条一批，批内持 kb 写锁、事务即开即提交——不在等模型响应期间占着写事务；
- 写回带状态守卫：只有"此刻仍为空答案"的行才更新，选题之后被别的工具（如 qsalvage 拆分）
  填上原文答案的题不会被模型答案覆盖。

    python -m mianjing_kb.answer_backfill --stats
    python -m mianjing_kb.answer_backfill --limit 300 --concurrency 4 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb import llm_client  # noqa: E402
from mianjing_kb.kb import config, db, wlock  # noqa: E402

SYSTEM = (
    "你是 Java/后端面试题答题助手。只输出答案正文，不要复述题目、不要加编号、不要客套。"
    "基于通用知识作答，控制在 150 字以内；若题目信息不足以确定要问什么、或明显不是一道题，"
    "只输出一行：NO_ANSWER"
)
MIN_Q_CHARS = 6
REFUSED = "llm_refused"
DRY_RUN_LIMIT = 20      # 不写库的预览默认上限：旧实现对全部目标调一遍模型再把结果丢掉
WRITE_BATCH = 50
# 状态守卫：选题与写回之间可能隔很久（等模型），期间答案可能已被别的工具补上。
_UPDATE_GUARDED = (
    "UPDATE questions SET a_text=?, answer_source=?, answer_model=?, answered_at=? "
    "WHERE qid=? AND TRIM(COALESCE(a_text,''))='' "
    "AND COALESCE(answer_source,'') IN ('', 'llm_error')"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def pick_targets(conn, limit: int = 0, states: tuple[str, ...] | None = None) -> list[tuple]:
    """待补答：空答案、未留痕（或历史遗留的 llm_error）、非目录伪题。"""
    states = states or ("boundary_gap", "no_answer_source", "ok")
    marks = ",".join("?" * len(states))
    sql = ("SELECT qid, q_text FROM questions "
           "WHERE TRIM(COALESCE(a_text,''))='' "
           f"AND (COALESCE(answer_source,'')='' OR COALESCE(answer_source,'')='llm_error') "
           f"AND COALESCE(answer_state,'') IN ({marks}) "
           "ORDER BY answer_state, qid")
    params = list(states)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def stats(conn) -> dict:
    rows = conn.execute(
        "SELECT COALESCE(answer_source,'') AS src, COUNT(*) FROM questions "
        "WHERE TRIM(COALESCE(a_text,''))='' GROUP BY src").fetchall()
    by = {k or "(原文空)": v for k, v in rows}
    left = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE TRIM(COALESCE(a_text,''))='' "
        "AND (COALESCE(answer_source,'')='' OR COALESCE(answer_source,'')='llm_error') "
        "AND COALESCE(answer_state,'') "
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
        out_dir: Path | None = None, lock=None) -> dict:
    """lock：返回写锁上下文的工厂（main 传入）；None 表示调用方自己保证独占（测试/脚本直接调用）。"""
    cfg = llm_client.resolve_config()
    todo = [(q, t) for q, t in qids if len((t or "").strip()) >= MIN_Q_CHARS]
    skipped_short = len(qids) - len(todo)
    stats_out = {"todo": len(qids), "skipped_short": skipped_short, "answered": 0,
                 "refused": 0, "error": 0, "written": 0, "guard_skipped": 0,
                 "elapsed_sec": 0, "model": cfg.get("model")}
    t0 = time.time()
    log = []
    pending: list[tuple] = []

    def flush() -> None:
        """一批写回：先拿写锁，事务只包这 50 条 UPDATE，不在等模型的间隙占着写事务。"""
        if dry_run or not pending:
            return
        with (lock() if lock else nullcontext()), conn:
            stats_out["written"] += conn.executemany(_UPDATE_GUARDED, pending).rowcount
        pending.clear()

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
            # 只有"模型给出终态"才写库：llm 写正文，llm_refused 记拒答（下轮不再重试）。
            # llm_error（网络/服务异常）不落库：留空让下一轮还能重试，
            # 否则一次 Ollama 掉线就把整批题记成"已完成"且永不重试（2026-11-24 code review P1-2）
            if src in (REFUSED, "llm"):
                pending.append((text, src, cfg.get("model"), _now(), q))
                if len(pending) >= WRITE_BATCH:
                    flush()
            if i % 200 == 0:
                print(json.dumps({"done": i, "of": len(todo), **{k: stats_out[k] for k in
                                  ("answered", "refused", "error")}}, ensure_ascii=False))
    flush()
    if not dry_run:
        # 守卫拦下的行：选题时为空、写回时已有答案（别的工具补的），不覆盖也不计入失败
        stats_out["guard_skipped"] = (stats_out["answered"] + stats_out["refused"]
                                      - stats_out["written"])
    stats_out["elapsed_sec"] = round(time.time() - t0, 1)
    stats_out["per_q_sec"] = round(stats_out["elapsed_sec"] / max(1, len(todo)), 2)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        # 追加写：断点续跑的多次记录不能互相覆盖（code review P2-8）
        with open(out_dir / "backfill-log.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            for r in log:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return stats_out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="空答案批量补答（本地模型，留痕可回滚）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--limit", type=int, default=None,
                    help="最多补 N 条（0=全部）。不写库的预览默认只跑 %d 条，全量预览请显式 --limit 0"
                         % DRY_RUN_LIMIT)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--apply", action="store_true", help="写库（默认只跑不写）")
    ap.add_argument("--stats", action="store_true", help="只看还剩多少、分布如何")
    ap.add_argument("--state", action="append", default=None,
                    help="只补这些 answer_state（默认 boundary_gap/no_answer_source/ok）")
    ap.add_argument("--out", default="", help="日志目录（默认 <build>/answer-backfill）")
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        # SQL 的 LIMIT -1 等于不限制：--limit -1 会把"预览"变成全量调用（ISSUE-07 同类）
        ap.error("--limit 必须大于等于 0（0 表示不限制）")
    apply = bool(args.apply)
    limit = args.limit if args.limit is not None else (0 if apply else DRY_RUN_LIMIT)
    path = Path(args.db)
    lock = (lambda: wlock.write_lock("kb", tool="answer_backfill", build_dir=path.parent,
                                     wait_sec=30.0, target=path)) if apply else None
    # 预览只读打开（不迁移、不写 WAL）；写库时连接（含 db.connect 内的迁移）也在写锁内完成，
    # 之后锁只在每批写回时短暂持有，不占着别人。
    try:
        if apply:
            with lock():
                conn = db.connect(path, create=False)
                db.migrate(conn)              # create=False 不自动迁移；迁移必须落在写锁内
        else:
            conn = db.connect(path, create=False, readonly=True)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False))
        return 2
    try:
        if args.stats:
            print(json.dumps(stats(conn), ensure_ascii=False))
            return 0
        targets = pick_targets(conn, limit, tuple(args.state) if args.state else None)
        print(json.dumps({"picked": len(targets), "limit": limit, "apply": apply},
                         ensure_ascii=False))
        if not targets:
            return 0
        out = Path(args.out or (path.parent / "answer-backfill"))
        try:
            res = run(conn, targets, concurrency=args.concurrency, dry_run=not apply,
                      out_dir=out, lock=lock)
        except wlock.LockHeld as ex:
            print(json.dumps({"verdict": "ABORT", "why": str(ex),
                              "note": "另有工具在写 kb.sqlite；已写回的批次保留，重跑会自动续上"},
                             ensure_ascii=False))
            return 2
        print(json.dumps(res, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
