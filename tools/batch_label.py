# -*- coding: utf-8 -*-
"""batch_label：LLM 批量打标全流程（docs/15 §3——staging 暂存，G6 通过才 merge）。

流程：
  --check            health：配置解析 + provider 可达 + 待打标数 + 预算估算（不花钱）
  （默认）           打标：topic 空/未分类的题 → 脱敏 → LLM → labels_staging
                     （--resume 按 staging 已有 qid 跳过；--budget 超了自动停；
                       单题 3 次失败落 build/label_failed.jsonl 不阻塞整批）
  --sample N         从 staging 随机抽 N 条打印（人工抽查，docs/15 G6 的 ≥0.8 准确率环节）
  --merge            G6 门禁复核 → PASS 才单事务 merge 进 questions（失败可整批回滚）

环境变量见 llm_client（LLM_PROVIDER/LLM_BASE/LLM_KEY/LLM_MODEL/LLM_CONCURRENCY/
LLM_PRICE/LLM_JSON_MODE——后两者可覆盖 provider 默认的价格与 json_object 支持开关）。

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
import os
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
    sql = ("SELECT q.qid, q.q_text, q.a_text, q.q_norm_hash FROM questions q "
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
    return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
            "topic": str(obj.get("topic") or "").strip()[:40],
            "qtype": obj.get("qtype"), "difficulty": obj.get("difficulty"),
            "tags": json.dumps(obj.get("tags") or [], ensure_ascii=False),
            "provider": cfg["provider"], "model": cfg["model"],
            "redacted": 1 if hit else 0, "latency_ms": r["latency_ms"],
            "tokens": r["tokens"],
            "prompt_tokens": r.get("prompt_tokens"),
            "completion_tokens": r.get("completion_tokens"),
            "labeled_at": datetime.now().strftime(NOW_FMT)}


def write_staging(conn, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO labels_staging "
        "(qid, q_norm_hash, topic, qtype, difficulty, tags, provider, model, "
        "redacted, latency_ms, tokens, prompt_tokens, completion_tokens, labeled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [[r[k] for k in ("qid", "q_norm_hash", "topic", "qtype", "difficulty", "tags",
                         "provider", "model", "redacted", "latency_ms", "tokens",
                         "prompt_tokens", "completion_tokens", "labeled_at")]
         for r in rows])
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

    done, failed, tokens_total, in_total, out_total = [], [], 0, 0, 0
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
            in_total += row.get("prompt_tokens") or 0
            out_total += row.get("completion_tokens") or 0
            if not dry_run and len(done) % 20 == 0:
                write_staging(conn, done[-20:])          # 每 20 题 flush（断点粒度）
            cost = (in_total * cfg["price"][0] + out_total * cfg["price"][1]) / 1e6
            if budget and cost > budget:
                for f in futs:
                    f.cancel()
                break                                    # 预算封顶，已完成的保留
    if done and not dry_run:
        write_staging(conn, done)
    return {"todo": len(todo), "labeled": len(done), "failed": len(failed),
            "tokens": tokens_total,
            "est_cost_yuan": round((in_total * cfg["price"][0]
                                    + out_total * cfg["price"][1]) / 1e6, 4),
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


def golden_sample(conn, n: int) -> dict:
    """批次二 G2：从 staging 按 topic 分层轮转抽样 → build/golden_todo.json。

    分层（每层轮转抽取）保证小主题不被大主题淹没——金标要能暴露弱项主题；
    无 q_norm_hash 的历史行跳过（金标以 hash 为键，跨 extract 重切存活）。
    人工把 items[].topic_gold/qtype_gold/difficulty_gold 填好后 --golden-load 回灌。
    """
    rows = conn.execute(
        "SELECT s.qid, s.q_norm_hash, s.topic, s.qtype, s.difficulty, s.tags, "
        "       q.q_text, q.a_text "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
        "WHERE s.q_norm_hash IS NOT NULL AND s.q_norm_hash != ''").fetchall()
    strata: dict[str, list[dict]] = {}
    for r in rows:
        strata.setdefault(r["topic"] or "", []).append(dict(r))
    rng = random.Random(42)
    keys = list(strata)
    rng.shuffle(keys)
    for k in keys:
        rng.shuffle(strata[k])
    picked: list[dict] = []
    i = 0
    while len(picked) < n and any(strata[k] for k in keys):
        k = keys[i % len(keys)]
        if strata[k]:
            picked.append(strata[k].pop())
        i += 1
    items = [{"qid": it["qid"], "q_norm_hash": it["q_norm_hash"],
              "q_text": (it["q_text"] or "")[:300], "a_text": (it["a_text"] or "")[:300],
              "current": {"topic": it["topic"], "qtype": it["qtype"],
                          "difficulty": it["difficulty"], "tags": it["tags"]},
              "topic_gold": "", "qtype_gold": "", "difficulty_gold": None}
             for it in picked]
    path = Path(config.BUILD_DIR) / "golden_todo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sampled": len(items), "items": items},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return {"sampled": len(items), "strata": len(strata), "path": str(path), "items": items}


def golden_load(conn, path, annotator: str = "human") -> dict:
    """批次二 G2：读回填好的 golden_todo.json → golden_labels（非法整批拒绝）。

    校验：q_norm_hash/topic_gold 非空、qtype_gold ∈ QTYPES 或空、
    difficulty_gold ∈ 1-5 或空；任何非法项整批拒绝（全部通过才写入）。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    errs: list[str] = []
    for i, it in enumerate(items):
        if not (it.get("q_norm_hash") or "").strip():
            errs.append(f"items[{i}]: q_norm_hash 为空")
        if not (it.get("topic_gold") or "").strip():
            errs.append(f"items[{i}]: topic_gold 为空")
        qg = (it.get("qtype_gold") or "").strip()
        if qg and qg not in QTYPES:
            errs.append(f"items[{i}]: qtype_gold 非法 {qg!r}（允许 {','.join(QTYPES)}）")
        dg = it.get("difficulty_gold")
        if dg is not None and dg not in (1, 2, 3, 4, 5):
            errs.append(f"items[{i}]: difficulty_gold 非法 {dg!r}（允许 1-5）")
    if errs:
        return {"ok": False, "errors": errs[:10], "rejected": len(errs),
                "note": "非法项整批拒绝（全部通过才写入）——修正后重跑"}
    now = datetime.now().strftime(NOW_FMT)
    conn.executemany(
        "INSERT OR REPLACE INTO golden_labels "
        "(q_norm_hash, q_text, topic_gold, qtype_gold, difficulty_gold, annotator, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [[it["q_norm_hash"].strip(), (it.get("q_text") or "")[:300],
          it["topic_gold"].strip(), (it.get("qtype_gold") or "").strip() or None,
          it.get("difficulty_gold"), annotator, now] for it in items])
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM golden_labels").fetchone()[0]
    return {"ok": True, "loaded": len(items), "total": total}


def merge(conn, drop_orphans: bool = False) -> dict:
    """G6 门禁复核 → FAIL 拒绝；PASS/WARN 放行（WARN=软门禁，提示人工抽查）。

    2026-11-19 审核修复（docs/项目审核报告/修复方案-2026-11-19.md）：
    - F6 孤儿行校验：qid 已失效或题干已变（q_norm_hash 不匹配）的 staging 行拒绝
      merge——防跨 extract 残留行命中 0 行仍被静默清掉；历史行 q_norm_hash 为
      NULL 放行（qid 对得上即可，向后兼容）。
    - F9 四列落库：topic/qtype/difficulty/tags；F3 空 topic/qtype 不覆盖已有值。
    - F1 打标事实落档 label_facts（同事务，按 q_norm_hash 跨重切存活，007）。
    - F4 next_step：merge 只写 kb.sqlite，检索库 topic 是 build_db 快照须 refresh。
    - 评审 Important#2（2026-11-19）：孤儿行拒绝时列出 qid（人工清理有抓手）；
      drop_orphans=True 清掉孤儿行后继续 merge——run_batch 跳过 staging 已有
      qid，重跑打标清不掉孤儿，没有这个出口主工作流会死锁。
    """
    from verify_ledger import g6_label
    g6 = g6_label(conn)
    if g6["state"] == "FAIL":
        return {"ok": False, "gate": "FAIL", "fails": g6["fails"][:5],
                "note": "G6 schema 合格率未过，staging 保留未 merge"}
    if g6["state"] == "SKIP":
        return {"ok": False, "gate": "SKIP", "note": "labels_staging 为空，无可 merge"}
    orphans = conn.execute(
        "SELECT COUNT(*) FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
        "WHERE q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash)"
    ).fetchone()[0]
    if orphans:
        orphan_qids = [r[0] for r in conn.execute(
            "SELECT s.qid FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
            "WHERE q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash) "
            "ORDER BY s.qid LIMIT 10").fetchall()]
        if not drop_orphans:
            return {"ok": False, "gate": g6["state"], "orphans": orphans,
                    "orphan_qids": orphan_qids,
                    "note": "staging 存在跨 extract 残留行（qid 已失效/题干已变），拒绝 merge——"
                            "重跑打标清不掉孤儿（已打 qid 会跳过），用 --merge --drop-orphans "
                            "清除后合并，或人工核对后手动清理"}
        conn.execute(
            "DELETE FROM labels_staging WHERE qid IN ("
            "  SELECT s.qid FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
            "  WHERE q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash))")
    n = conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    conn.execute(
        "UPDATE questions SET "
        " topic=COALESCE(NULLIF((SELECT s.topic FROM labels_staging s "
        "                        WHERE s.qid=questions.qid), ''), topic), "
        " qtype=COALESCE((SELECT s.qtype FROM labels_staging s WHERE s.qid=questions.qid), qtype), "
        " difficulty=(SELECT s.difficulty FROM labels_staging s WHERE s.qid=questions.qid), "
        " tags=(SELECT s.tags FROM labels_staging s WHERE s.qid=questions.qid) "
        "WHERE qid IN (SELECT qid FROM labels_staging)")
    conn.execute(
        "INSERT OR REPLACE INTO label_facts "
        "SELECT q.q_norm_hash, s.topic, s.qtype, s.difficulty, s.tags, ? "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
        "WHERE COALESCE(s.topic, '') != ''",
        (datetime.now().strftime(NOW_FMT),))
    conn.execute("DELETE FROM labels_staging")
    conn.commit()
    out = {"ok": True, "merged": n, "gate": g6["state"],
           "next_step": "python tools/refresh.py --reembed"
                        "  # 检索库 topic 是快照，merge 只写 kb.sqlite（B2）"}
    if orphans:
        out["dropped_orphans"] = orphans
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
    ap.add_argument("--drop-orphans", action="store_true",
                    help="与 --merge 连用：清除 staging 孤儿行（qid 已失效/题干已变）后继续合并")
    # 批次二 G2（2026-11-19）：金标层——分层抽样出待人工标注清单 + 回灌落库
    ap.add_argument("--golden-sample", type=int, default=0,
                    help="从 staging 按 topic 分层抽 N 条 → build/golden_todo.json（人工填 gold）")
    ap.add_argument("--golden-load", default="",
                    help="回灌填好的 golden_todo.json → golden_labels（非法项整批拒绝）")
    ap.add_argument("--annotator", default="human", help="金标标注人（--golden-load 用）")
    # F10（2026-11-19）：docs/15 §3.5 承诺的三旗标——此前 argparse 未声明，
    # 照文档敲命令报 unrecognized arguments。
    ap.add_argument("--provider", choices=("local", "online"),
                    help="LLM provider（写入 LLM_PROVIDER 后再解析配置）")
    ap.add_argument("--concurrency", type=int, default=0,
                    help="并发数（写入 LLM_CONCURRENCY 后再解析配置；0=不覆盖）")
    ap.add_argument("--resume", action="store_true",
                    help="兼容旗标：续跑是默认行为（staging 已有 qid 自动跳过），无需显式传")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)

    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.concurrency:
        os.environ["LLM_CONCURRENCY"] = str(args.concurrency)

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
        if args.golden_sample:
            out = golden_sample(conn, args.golden_sample)
            out.pop("items")                 # 清单在文件里，控制台只报摘要
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0
        if args.golden_load:
            out = golden_load(conn, Path(args.golden_load), annotator=args.annotator)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        if args.merge:
            out = merge(conn, drop_orphans=args.drop_orphans)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        out = run_batch(cfg, conn, limit=args.limit, budget=args.budget, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
