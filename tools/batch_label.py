# -*- coding: utf-8 -*-
"""batch_label：LLM 批量打标全流程（docs/15 §3——staging 暂存，G6 通过才 merge）。

流程：
  --check            health：配置解析 + provider 可达 + 待打标数 + 预算估算（不花钱）
  （默认）           打标：topic 空/未分类的题 → 脱敏 → LLM → labels_staging
                     （--resume 按 staging 已有 qid 跳过；--budget 超了自动停；
                       单题 3 次失败落 build/label_failed.jsonl 不阻塞整批）
  --sample N         从 staging 随机抽 N 条打印（人工抽查，docs/15 G6 的 ≥0.8 准确率环节）
  --merge            G6 门禁复核 → PASS 才单事务 merge 进 questions（失败可整批回滚）

环境变量见 llm_client（LLM_PROVIDER/LLM_BASE/LLM_KEY/LLM_MODEL/LLM_CONCURRENCY）。

用法：
    python tools/batch_label.py --check
    python tools/batch_label.py --dry-run --limit 5      # 只打 5 题进 staging 试输出
    python tools/batch_label.py                          # 全量（断点续跑，预算封顶）
    python tools/batch_label.py --sample 50              # 人工抽查清单
    python tools/batch_label.py --merge                  # G6 复核 + 落库
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db  # noqa: E402
from llm_client import ConfigError, chat, chat_json, redact, resolve_config  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
QTYPES = ("八股", "算法", "HR", "场景", "项目")
SYSTEM = ("你是面试题标注器。阅读题目与答案片段，只输出 JSON 对象，格式："
          '{"topic": <2-8字主题，如 "Java基础"/"MySQL"/"Redis">, '
          '"qtype": <从 八股/算法/HR/场景/项目 中选恰好一个>, '
          '"difficulty": <1-5整数>, "tags": [<关键词>, ...]}')


def pick_targets(conn, limit: int = 0) -> list[dict]:
    """待打标：题级 topic 为空的题（qid, 题干, 答案前 300 字——docs/15 §3.5 裁切口径）。

    优先级：文档级主题弱（空/综合/未分类）的题先打——价值最高（docs/15 §3.5 的
    "先做 4,011 综合级"精神）；文档级主题强的靠后（题级打标是锦上添花）。
    """
    sql = ("SELECT q.qid, q.q_text, q.a_text FROM questions q "
           "LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash "
           "WHERE q.topic IS NULL OR q.topic='' "
           "ORDER BY (sd.topic IS NULL OR sd.topic='' OR sd.topic='综合' OR sd.topic='未分类') DESC, "
           "q.qid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def label_one(cfg: dict, q: dict) -> dict:
    """单题打标：脱敏 → LLM → staging 行（失败抛异常，调用方落 failed 队列）。"""
    text = f"{q['q_text'][:300]}\n{q['a_text'][:300]}"
    clean, hit = redact(text)
    r = chat_json(cfg, SYSTEM, f"题目与答案片段：\n{clean}", max_tokens=128)
    obj = r.get("obj")
    if obj is None:
        raise RuntimeError("JSON 解析失败")
    return {"qid": q["qid"], "topic": str(obj.get("topic") or "").strip()[:40],
            "qtype": obj.get("qtype"), "difficulty": obj.get("difficulty"),
            "tags": json.dumps(obj.get("tags") or [], ensure_ascii=False),
            "provider": cfg["provider"], "model": cfg["model"],
            "redacted": 1 if hit else 0, "latency_ms": r["latency_ms"],
            "tokens": r["tokens"], "labeled_at": datetime.now().strftime(NOW_FMT)}


def write_staging(conn, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO labels_staging "
        "(qid, topic, qtype, difficulty, tags, provider, model, redacted, latency_ms, tokens, labeled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [[r[k] for k in ("qid", "topic", "qtype", "difficulty", "tags", "provider", "model",
                         "redacted", "latency_ms", "tokens", "labeled_at")] for r in rows])
    conn.commit()
    return len(rows)


def health(cfg: dict, conn) -> dict:
    """--check：配置 + 连通 + 待打标数 + 预算估算。"""
    out = {"provider": cfg["provider"], "model": cfg["model"], "base": cfg["base"]}
    try:
        r = chat(cfg, "你是测试助手，只回一个字。", "回复：好", max_tokens=8, retries=1, sleep=0)
        out["reachable"] = bool(r["text"].strip())
        out["latency_ms"] = r["latency_ms"]
    except Exception as ex:  # noqa: BLE001
        out["reachable"] = False
        out["error"] = f"{type(ex).__name__}: {str(ex)[:140]}"
    n = conn.execute("SELECT COUNT(*) FROM questions "
                     "WHERE topic IS NULL OR topic='' OR topic='未分类'").fetchone()[0]
    staged = conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    out["todo"] = n
    out["staged"] = staged
    # 预算估算（docs/15 §3.5：单题 ≈420 in + 40 out tokens）
    if cfg["price"] != (0.0, 0.0):
        out["est_cost_yuan"] = round(n * (420 * cfg["price"][0] + 40 * cfg["price"][1]) / 1e6, 2)
    return out


def run_batch(cfg: dict, conn, *, limit: int = 0, budget: float = 10.0,
              dry_run: bool = False, failed_path: Path | None = None) -> dict:
    """并发打标 → staging。resume：staging 已有 qid 跳过；budget：估算成本超限即停。"""
    targets = pick_targets(conn, limit)
    staged = {r[0] for r in conn.execute("SELECT qid FROM labels_staging").fetchall()}
    todo = [q for q in targets if q["qid"] not in staged]
    failed_path = failed_path or (Path(config.BUILD_DIR) / "label_failed.jsonl")
    failed_path.parent.mkdir(parents=True, exist_ok=True)

    done, failed, tokens_total = [], [], 0
    started = time.perf_counter()

    def _worker(q):
        try:
            return q, label_one(cfg, q), None
        except Exception as ex:  # noqa: BLE001 —— 单题失败不阻塞整批（docs/15 §3.4）
            return q, None, ex

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        futs = [pool.submit(_worker, q) for q in todo]
        for fut in as_completed(futs):
            q, row, exc = fut.result()
            if exc is not None:
                failed.append({"qid": q["qid"],
                               "err": f"{type(exc).__name__}: {str(exc)[:160]}",
                               "at": datetime.now().strftime(NOW_FMT)})
                with failed_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(failed[-1], ensure_ascii=False) + "\n")
                continue
            done.append(row)
            tokens_total += row["tokens"]
            if not dry_run and len(done) % 20 == 0:
                write_staging(conn, done[-20:])          # 每 20 题 flush（断点粒度）
            cost = tokens_total * sum(cfg["price"]) / 1e6
            if budget and cost > budget:
                for f in futs:
                    f.cancel()
                break                                    # 预算封顶，已完成的保留
    if done and not dry_run:
        write_staging(conn, done)
    return {"todo": len(todo), "labeled": len(done), "failed": len(failed),
            "tokens": tokens_total,
            "est_cost_yuan": round(tokens_total * sum(cfg["price"]) / 1e6, 4),
            "dry_run": dry_run, "budget": budget,
            "elapsed_sec": round(time.perf_counter() - started, 1),
            "failed_log": str(failed_path) if failed else None}


def sample_staging(conn, n: int) -> list[dict]:
    """随机抽 staging n 条（人工抽查清单，docs/15 G6 抽检环节）。"""
    total = conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    if total == 0:
        return []
    rows = conn.execute(
        "SELECT s.qid, q.q_text, s.topic, s.qtype, s.difficulty, s.tags, s.redacted "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid").fetchall()
    k = min(n, total)
    return [dict(r) for r in random.Random(42).sample(list(rows), k)]


def merge(conn) -> dict:
    """G6 门禁复核 → FAIL 拒绝；PASS/WARN 放行（WARN=软门禁，提示人工抽查）。"""
    from verify_ledger import g6_label
    g6 = g6_label(conn)
    if g6["state"] == "FAIL":
        return {"ok": False, "gate": "FAIL", "fails": g6["fails"][:5],
                "note": "G6 schema 合格率未过，staging 保留未 merge"}
    if g6["state"] == "SKIP":
        return {"ok": False, "gate": "SKIP", "note": "labels_staging 为空，无可 merge"}
    n = conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    conn.execute("UPDATE questions SET topic=(SELECT topic FROM labels_staging "
                 "WHERE labels_staging.qid=questions.qid) "
                 "WHERE qid IN (SELECT qid FROM labels_staging)")
    conn.execute("DELETE FROM labels_staging")
    conn.commit()
    out = {"ok": True, "merged": n, "gate": g6["state"]}
    if g6["state"] == "WARN":
        out["note"] = "topic 漂移 WARN（软门禁不阻断）——建议 --sample 50 人工抽查确认"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LLM 批量打标（staging + G6 门禁 + merge）")
    ap.add_argument("--check", action="store_true", help="health：配置/连通/待打标数/预算估算")
    ap.add_argument("--dry-run", action="store_true", help="打标但不写 staging（试输出）")
    ap.add_argument("--limit", type=int, default=0, help="最多打标 N 题（试跑）")
    ap.add_argument("--budget", type=float, default=10.0, help="预算封顶 ¥（超了自动停，docs/15 §3.5）")
    ap.add_argument("--sample", type=int, default=0, help="从 staging 随机抽 N 条打印（人工抽查）")
    ap.add_argument("--merge", action="store_true", help="G6 复核 + merge 进 questions")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)

    try:
        cfg = resolve_config()
    except ConfigError as ex:
        print(json.dumps({"fatal": str(ex)}, ensure_ascii=False, indent=1))
        return 2

    conn = db.connect(args.db)      # create=True：幂等跑 005 staging / 006 题级 topic 迁移
    try:
        if args.check:
            print(json.dumps(health(cfg, conn), ensure_ascii=False, indent=1))
            return 0
        if args.sample:
            rows = sample_staging(conn, args.sample)
            for r in rows:
                print(f"  qid={r['qid']:<6} topic={r['topic']:<10} qtype={r['qtype']:<4} "
                      f"diff={r['difficulty']} redact={r['redacted']} | {r['q_text'][:50]}")
            print(json.dumps({"sampled": len(rows)}, ensure_ascii=False))
            return 0
        if args.merge:
            out = merge(conn)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        out = run_batch(cfg, conn, limit=args.limit, budget=args.budget, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
