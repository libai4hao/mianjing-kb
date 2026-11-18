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
    python -m mianjing_kb.batch_label --check
    python -m mianjing_kb.batch_label --dry-run --limit 5      # 只打 5 题进 staging 试输出
    python -m mianjing_kb.batch_label                          # 全量（断点续跑，预算封顶）
    python -m mianjing_kb.batch_label --sample 50              # 人工抽查清单
    python -m mianjing_kb.batch_label --merge                  # G6 复核 + 落库
"""
from __future__ import annotations

import argparse
import csv
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
from mianjing_kb.kb import config, db, wlock  # noqa: E402
from mianjing_kb.kb.vocab import OTHER_TOPIC, topic_vocab  # noqa: E402
from mianjing_kb.llm_client import ConfigError, chat, chat_json, redact, resolve_config  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
QTYPES = ("八股", "算法", "HR", "场景", "项目")
SYSTEM = ("你是面试题标注器。阅读题目与答案片段，只输出 JSON 对象，格式："
          '{"topic": <2-8字主题，如 "Java基础"/"MySQL"/"Redis">, '
          '"qtype": <从 八股/算法/HR/场景/项目 中选恰好一个>, '
          '"difficulty": <1-5整数>, "tags": [<关键词>, ...]}')
# 批次三 R2：词表非空时换锁枚举版（docs/13 §5——封闭词表防主题碎片化）
VOCAB_SYSTEM = ("你是面试题标注器。阅读题目与答案片段，只输出 JSON 对象，格式："
                '{{"topic": <主题，必须从词表中选恰好一个：{vocab}；拿不准选其他>, '
                '"qtype": <从 八股/算法/HR/场景/项目 中选恰好一个>, '
                '"difficulty": <1-5整数>, "tags": [<关键词>, ...]}}')
# 批次三 R5（bug5）：待打标口径单源——pick_targets 与 health 共用同一谓词，
# 防"未分类算不算待打标"两处漂移（审核报告 bug5）。
TODO_PREDICATE = (f"(q.topic IS NULL OR q.topic = '' "
                  f"OR q.topic = '{config.UNCLASSIFIED}')")


def pick_targets(conn, limit: int = 0) -> list[dict]:
    """待打标：题级 topic 为空/未分类的题（口径=TODO_PREDICATE，docs/15 §3.5 裁切口径）。

    优先级：文档级主题弱（空/综合/未分类）的题先打——价值最高（docs/15 §3.5 的
    "先做 4,011 综合级"精神）；文档级主题强的靠后（题级打标是锦上添花）。
    """
    sql = ("SELECT q.qid, q.q_text, q.a_text, q.q_norm_hash FROM questions q "
           "LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash "
           f"WHERE {TODO_PREDICATE} "
           "ORDER BY (sd.topic IS NULL OR sd.topic='' OR sd.topic='综合' OR sd.topic='未分类') DESC, "
           "q.qid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def label_one(cfg: dict, q: dict) -> dict:
    """单题打标：脱敏 → LLM → staging 行（失败抛异常，调用方落 failed 队列）。

    批次三 R2：词表非空时 SYSTEM 换锁枚举版，且落表前兜底——topic 不在词表
    改"其他" + review_state='pending'（prompt 锁 + 落表双保险）；词表降级
    （空元组）走旧口径（自由主题，review_state=None）。
    """
    vocab = topic_vocab()
    system = VOCAB_SYSTEM.format(vocab="、".join(vocab)) if vocab else SYSTEM
    text = f"{q['q_text'][:300]}\n{q['a_text'][:300]}"
    clean, hit = redact(text)
    r = chat_json(cfg, system, f"题目与答案片段：\n{clean}", max_tokens=128)
    obj = r.get("obj")
    if obj is None:
        raise RuntimeError("JSON 解析失败")
    topic = str(obj.get("topic") or "").strip()[:40]
    review_state = None
    if vocab and topic not in vocab:
        topic = OTHER_TOPIC
        review_state = "pending"
    return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
            "topic": topic,
            "qtype": obj.get("qtype"), "difficulty": obj.get("difficulty"),
            "tags": json.dumps(obj.get("tags") or [], ensure_ascii=False),
            "provider": cfg["provider"], "model": cfg["model"],
            "redacted": 1 if hit else 0, "latency_ms": r["latency_ms"],
            "tokens": r["tokens"],
            "prompt_tokens": r.get("prompt_tokens"),
            "completion_tokens": r.get("completion_tokens"),
            "review_state": review_state,
            "labeled_at": datetime.now().strftime(NOW_FMT)}


def write_staging(conn, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO labels_staging "
        "(qid, q_norm_hash, topic, qtype, difficulty, tags, provider, model, "
        "redacted, latency_ms, tokens, prompt_tokens, completion_tokens, labeled_at, review_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [[r.get(k) for k in ("qid", "q_norm_hash", "topic", "qtype", "difficulty", "tags",
                             "provider", "model", "redacted", "latency_ms", "tokens",
                             "prompt_tokens", "completion_tokens", "labeled_at", "review_state")]
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
    n = conn.execute(f"SELECT COUNT(*) FROM questions q WHERE {TODO_PREDICATE}").fetchone()[0]
    staged = conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    out["todo"] = n
    out["staged"] = staged
    # 预算估算（docs/15 §3.5：单题 ≈420 in + 40 out tokens）
    if cfg["price"] != (0.0, 0.0):
        out["est_cost_yuan"] = round(n * (420 * cfg["price"][0] + 40 * cfg["price"][1]) / 1e6, 2)
    return out


FAILED_CSV_HEADER = ["qid", "err_type", "attempts", "last_at", "model", "err"]


def write_failed_csv(failed_path: Path) -> tuple[Path, int]:
    """label_failed.jsonl → label_failed.csv：一题一行的人工待办清单。

    jsonl 只追加、是明细；csv 每次重跑重建，按 qid 聚合出错误类型、累计失败次数、最后模型。
    """
    path = failed_path.with_suffix(".csv")
    agg: dict[str, dict] = {}
    if failed_path.exists():
        for line in failed_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            qid = str(rec.get("qid"))
            a = agg.setdefault(qid, {"qid": qid, "attempts": 0, "err_type": "-",
                                     "last_at": "-", "model": "-", "err": ""})
            a["attempts"] += 1
            a["last_at"] = rec.get("at") or a["last_at"]
            a["model"] = rec.get("model") or a["model"]
            a["err_type"] = rec.get("err_type") or a["err_type"]
            a["err"] = (rec.get("err") or a["err"])[:200]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FAILED_CSV_HEADER)
        w.writeheader()
        key = lambda r: (0, int(r["qid"])) if r["qid"].isdigit() else (1, r["qid"])
        for row in sorted(agg.values(), key=key):
            w.writerow({k: row[k] for k in FAILED_CSV_HEADER})
    return path, len(agg)


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
                failed.append({"qid": q["qid"], "err_type": type(exc).__name__,
                               "err": f"{type(exc).__name__}: {str(exc)[:160]}",
                               "model": cfg["model"],
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
    failed_csv, failed_qids = (write_failed_csv(failed_path) if failed else (None, 0))
    return {"todo": len(todo), "labeled": len(done), "failed": len(failed),
            "tokens": tokens_total,
            "est_cost_yuan": round((in_total * cfg["price"][0]
                                    + out_total * cfg["price"][1]) / 1e6, 4),
            "dry_run": dry_run, "budget": budget,
            "elapsed_sec": round(time.perf_counter() - started, 1),
            "failed_log": str(failed_path) if failed else None,
            "failed_csv": str(failed_csv) if failed else None,
            "failed_qids": failed_qids}


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
    if path.exists():            # 评审 Important#4：已有未回灌清单 → 拒绝重抽防覆盖
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("items"):
                return {"ok": False, "path": str(path),
                        "note": f"已存在待回灌金标文件（{len(old['items'])} 条）——先 "
                                "--golden-load 回灌，或删除/改名该文件后重抽"
                                "（防覆盖人工已填内容）"}
        except Exception:  # noqa: BLE001 —— 损坏文件不挡重抽
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sampled": len(items), "items": items},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return {"sampled": len(items), "strata": len(strata), "path": str(path), "items": items}


def golden_load(conn, path, annotator: str = "human") -> dict:
    """批次二 G2：读回填好的 golden_todo.json → golden_labels（非法整批拒绝）。

    校验：q_norm_hash/topic_gold 非空、qtype_gold ∈ QTYPES 或空、
    difficulty_gold ∈ 1-5 或空；任何非法项整批拒绝（全部通过才写入）。
    回灌成功后把清单文件改名 golden_todo.loaded-<ts>.json 留档（评审 Important#4）
    ——此后 --golden-sample 可自由重抽，不会撞覆盖守卫。
    """
    p = Path(path)
    if not p.exists():           # 评审 Minor#2：文件缺失/坏 JSON/顶层非对象优雅拒绝
        return {"ok": False, "errors": [f"文件不存在: {p}"],
                "note": "先 --golden-sample 生成清单，人工填写后再回灌"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return {"ok": False, "errors": [f"JSON 读取失败: {ex}"],
                "note": "检查文件编码/内容后重试"}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"ok": False, "errors": ["顶层必须是含 items 数组的 JSON 对象"],
                "note": "清单格式见 --golden-sample 输出"}
    items = data["items"]
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
    archived = None
    try:                         # 评审 Important#4：回灌成功后清单改名留档 → 可自由重抽
        archived = p.with_name(f"golden_todo.loaded-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        p.rename(archived)
    except OSError:
        archived = None          # 留档失败不影响回灌结果
    return {"ok": True, "loaded": len(items), "total": total,
            "archived_to": str(archived) if archived else None}


def eval_golden(conn) -> dict:
    """批次二 G3 --eval：调 G6b 金标抽检 → build/label_eval.json（全量矩阵+弱项）。"""
    from mianjing_kb.verify_ledger import g6b_golden
    g = g6b_golden(conn)
    path = Path(config.BUILD_DIR) / "label_eval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"state": g["state"], "detail": g["detail"], "report": str(path)},
                     ensure_ascii=False, indent=1))
    return g


# ---- 批次三 R3：复核执行层（2026-11-19，docs/项目审核报告/修复方案-2026-11-19.md §八）----

REVIEW_SYSTEM = (
    "你是面试题标注复核员。给出题目、答案片段与当前标签，判断标签是否正确。"
    "topic 必须从词表选恰好一个：{vocab}；拿不准给建议主题并说明理由。"
    '只输出 JSON 对象：{{"verdict": <agree 或 disagree>, '
    '"topic": <建议主题，agree 时可省>, "qtype": <八股/算法/HR/场景/项目>, '
    '"difficulty": <1-5整数>, "note": <一句话理由>}}'
)


def review_targets(conn, limit: int = 0) -> list[dict]:
    """三类复核目标：词表外（含兜底"其他"）/ 低置信（conf low/none）/ 金标弱区。

    金标弱区 = 金标对照判定该行 topic 不符（按行 EXISTS；评审 Important#2：
    与 G6b 按金标主题聚合的 weak_topics 同源——错行混进高准确率预测组也必进复核）；
    review_state 非 NULL 的已复核行不重选。
    """
    vocab = topic_vocab()
    params: list = []
    if vocab:
        ph = ",".join("?" for _ in vocab)
        out_cond = f"(s.topic NOT IN ({ph}) OR s.topic = ?)"
        params.extend(vocab)
        params.append(OTHER_TOPIC)
    else:                        # 词表降级：只盯兜底"其他"
        out_cond = "s.topic = ?"
        params.append(OTHER_TOPIC)
    weak_cond = ("EXISTS (SELECT 1 FROM golden_labels g "
                 "WHERE g.q_norm_hash = s.q_norm_hash "
                 "  AND COALESCE(s.topic, '') != g.topic_gold)")
    sql = ("SELECT s.qid, s.topic, s.qtype, s.difficulty, s.tags, "
           "       q.q_text, q.a_text, q.conf "
           "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
           "WHERE s.review_state IS NULL "
           f"  AND ({out_cond} OR q.conf IN ('low','none') OR {weak_cond}) "
           "ORDER BY s.qid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def review_one(cfg: dict, row: dict) -> dict:
    """单题复核：LLM 判 agree/disagree，失败抛异常（run_review 记 failed 不阻塞）。"""
    vocab = topic_vocab()
    vocab_hint = "、".join(vocab) if vocab else "（词表不可用，给 2-8 字主题建议）"
    system = REVIEW_SYSTEM.format(vocab=vocab_hint)
    text = (f"题目：{(row['q_text'] or '')[:300]}\n"
            f"答案片段：{(row['a_text'] or '')[:200]}\n"
            f"当前标签：topic={row['topic']} qtype={row['qtype']} "
            f"difficulty={row['difficulty']} tags={row['tags']}")
    clean, _ = redact(text)
    r = chat_json(cfg, system, clean, max_tokens=160)
    obj = r.get("obj")
    if obj is None:
        raise RuntimeError(f"JSON 解析失败: {str(r.get('raw'))[:80]}")
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("agree", "disagree"):
        raise RuntimeError(f"verdict 非法: {verdict!r}")
    return {"verdict": verdict,
            "suggestion": {"topic": obj.get("topic"), "qtype": obj.get("qtype"),
                           "difficulty": obj.get("difficulty")},
            "note": str(obj.get("note") or "")[:200],
            "review_model": cfg["model"],
            "prompt_tokens": r.get("prompt_tokens") or 0,
            "completion_tokens": r.get("completion_tokens") or 0}


def run_review(cfg, conn, *, limit: int = 0, budget: float = 10.0,
               failed_path: Path | None = None) -> dict:
    """LLM 复核三类可疑标签 → review_state/review_model/review_note。

    agree → 'agree'（review_note 存理由）；disagree → 'disagree'（review_note 存
    建议 JSON）——分歧行 merge 不落库，人工 --review-resolve 终审后放行；
    单题失败不阻塞（build/review_failed.jsonl）。--limit/--budget 与打标共用。
    """
    targets = review_targets(conn, limit)
    failed_path = failed_path or (Path(config.BUILD_DIR) / "review_failed.jsonl")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    done, failed, in_total, out_total = 0, [], 0, 0
    started = time.perf_counter()

    def _worker(row):
        try:
            return row, review_one(cfg, row), None
        except Exception as ex:              # noqa: BLE001 —— 单题失败不阻塞
            return row, None, ex

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        futs = [pool.submit(_worker, t) for t in targets]
        for fut in as_completed(futs):
            row, res, exc = fut.result()
            if exc is not None:
                failed.append({"qid": row["qid"],
                               "err": f"{type(exc).__name__}: {str(exc)[:160]}",
                               "at": datetime.now().strftime(NOW_FMT)})
                with failed_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(failed[-1], ensure_ascii=False) + "\n")
                continue
            if res["verdict"] == "agree":
                note = res["note"]
            else:
                note = json.dumps({"suggestion": res["suggestion"], "note": res["note"]},
                                  ensure_ascii=False)
            conn.execute("UPDATE labels_staging SET review_state=?, review_model=?, "
                         "review_note=? WHERE qid=?",
                         (res["verdict"], res["review_model"], note, row["qid"]))
            conn.commit()
            done += 1
            in_total += res["prompt_tokens"]
            out_total += res["completion_tokens"]
            cost = (in_total * cfg["price"][0] + out_total * cfg["price"][1]) / 1e6
            if budget and cost > budget:
                for f in futs:
                    f.cancel()
                break
    # agree/disagree 为全表累计（含此前轮次）；targets/reviewed 是本轮口径（评审 Minor#3）
    agree = conn.execute("SELECT COUNT(*) FROM labels_staging "
                         "WHERE review_state='agree'").fetchone()[0]
    disagree = conn.execute("SELECT COUNT(*) FROM labels_staging "
                            "WHERE review_state='disagree'").fetchone()[0]
    return {"targets": len(targets), "reviewed": done, "failed": len(failed),
            "agree": agree, "disagree": disagree,
            "est_cost_yuan": round((in_total * cfg["price"][0]
                                    + out_total * cfg["price"][1]) / 1e6, 4),
            "elapsed_sec": round(time.perf_counter() - started, 1),
            "failed_log": str(failed_path) if failed else None}


def review_list(conn) -> list[dict]:
    """待人工终审清单（disagree 优先，pending 兜底；评审 Important#1：LEFT JOIN
    ——题干已失效的分歧孤儿行也列出，终审唯一出口 --review-resolve）。"""
    rows = conn.execute(
        "SELECT s.qid, s.topic, s.qtype, s.difficulty, s.review_state, s.review_note, "
        "       COALESCE(q.q_text, '(题干已失效)') AS q_text "
        "FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
        "WHERE s.review_state IN ('disagree','pending') "
        "ORDER BY CASE WHEN s.review_state='disagree' THEN 0 ELSE 1 END, s.qid").fetchall()
    return [dict(r) for r in rows]


def review_resolve(conn, spec: str) -> dict:
    """人工终审 "qid:action ..."（keep/suggest/drop）——merge 分歧行的唯一出口。

    keep=维持原标签（review_state='agree'）；suggest=把 review_note 里的建议
    应用到标签列再 'agree'；drop=DELETE 行。未知/无行 token 进 unknown 上报。
    """
    resolved, unknown = 0, []
    for token in spec.split():
        try:
            qid_s, action = token.split(":", 1)
            qid = int(qid_s)
        except ValueError:
            unknown.append(token)
            continue
        row = conn.execute("SELECT * FROM labels_staging WHERE qid=?", (qid,)).fetchone()
        if row is None:
            unknown.append(token)
            continue
        if action == "keep":
            conn.execute("UPDATE labels_staging SET review_state='agree' WHERE qid=?", (qid,))
        elif action == "suggest":
            try:
                sug = (json.loads(row["review_note"]) or {}).get("suggestion") or {}
            except (json.JSONDecodeError, TypeError):
                unknown.append(token)
                continue
            conn.execute(
                "UPDATE labels_staging SET "
                "topic=COALESCE(NULLIF(?,''),topic), qtype=COALESCE(?,qtype), "
                "difficulty=COALESCE(?,difficulty), review_state='agree' WHERE qid=?",
                (str(sug.get("topic") or "").strip()[:40], sug.get("qtype"),
                 sug.get("difficulty"), qid))
        elif action == "drop":
            conn.execute("DELETE FROM labels_staging WHERE qid=?", (qid,))
        else:
            unknown.append(token)
            continue
        resolved += 1
    conn.commit()
    return {"resolved": resolved, "unknown": unknown}


def merge(conn, drop_orphans: bool = False) -> dict:
    """G6 门禁复核 → FAIL 拒绝；PASS/WARN 放行（WARN=软门禁，提示人工抽查）。

    2026-11-19 审核修复（docs/项目审核报告/修复方案-2026-11-19.md）：
    - F6 孤儿行校验：qid 已失效或题干已变（q_norm_hash 不匹配）的 staging 行拒绝
      merge——防跨 extract 残留行命中 0 行仍被静默清掉；历史行 q_norm_hash 为
      NULL 放行（qid 对得上即可，向后兼容）。
    - F9 四列落库：topic/qtype/difficulty/tags；F3 空 topic/qtype 不覆盖已有值。
    - F1 打标事实落档 label_facts（同事务，按 q_norm_hash 跨重切存活，007）。
    - F4 next_step：merge 只写 kb.sqlite，检索库 topic 是 build_db 快照须 refresh。
    - 批次三 R3：review_state='disagree' 的分歧行不落库、不落 facts、不删——
      人工 --review-resolve 终审后（agree）下次 merge 自然放行。
    - 批次三 R5（评审 Minor#1）：tags 空值守卫——staging tags '[]'（LLM 没给）
      不覆盖 questions 已有 tags，防第二轮抹第一轮；difficulty NULL 同守卫
      （G6 现拦 NULL，防御性保留）。
    - 评审 Important#2（2026-11-19）：孤儿行拒绝时列出 qid（人工清理有抓手）；
      drop_orphans=True 清掉孤儿行后继续 merge——run_batch 跳过 staging 已有
      qid，重跑打标清不掉孤儿，没有这个出口主工作流会死锁。
    - 评审 Important#1（2026-11-19）：分歧行不算孤儿——--drop-orphans 不误删
      人工分歧（终审唯一出口 --review-resolve）；G6b 拒绝时 gate 标 "G6b"。
    """
    from mianjing_kb.verify_ledger import g6_label, g6b_golden
    g6 = g6_label(conn)
    if g6["state"] == "FAIL":
        return {"ok": False, "gate": "FAIL", "fails": g6["fails"][:5],
                "note": "G6 schema 合格率未过，staging 保留未 merge"}
    if g6["state"] == "SKIP":
        return {"ok": False, "gate": "SKIP", "note": "labels_staging 为空，无可 merge"}
    g6b = g6b_golden(conn)
    if g6b["state"] == "FAIL":
        weak = ",".join(g6b["detail"].get("weak_topics") or []) or "-"
        return {"ok": False, "gate": "G6b", "g6b": g6b["detail"],
                "note": f"金标抽检准确率 <0.8（G6b FAIL，弱项主题：{weak}），"
                        "staging 保留未 merge——--eval 看混淆矩阵，修标签或补金标后重试"}
    orphans = conn.execute(
        "SELECT COUNT(*) FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
        "WHERE (q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash)) "
        "  AND s.review_state IS NOT 'disagree'"    # 评审 Important#1：分歧行不算孤儿
    ).fetchone()[0]
    if orphans:
        orphan_qids = [r[0] for r in conn.execute(
            "SELECT s.qid FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
            "WHERE (q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash)) "
            "  AND s.review_state IS NOT 'disagree' "
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
            "  WHERE (q.qid IS NULL OR (s.q_norm_hash IS NOT NULL AND s.q_norm_hash != q.q_norm_hash)) "
            "    AND s.review_state IS NOT 'disagree')")
    n = conn.execute("SELECT COUNT(*) FROM labels_staging "
                     "WHERE review_state IS NOT 'disagree'").fetchone()[0]
    conn.execute(
        "UPDATE questions SET "
        " topic=COALESCE(NULLIF((SELECT s.topic FROM labels_staging s "
        "                        WHERE s.qid=questions.qid), ''), topic), "
        " qtype=COALESCE((SELECT s.qtype FROM labels_staging s WHERE s.qid=questions.qid), qtype), "
        " difficulty=COALESCE((SELECT s.difficulty FROM labels_staging s WHERE s.qid=questions.qid), "
        "difficulty), "
        " tags=COALESCE(NULLIF((SELECT s.tags FROM labels_staging s WHERE s.qid=questions.qid), "
        "'[]'), tags) "
        "WHERE qid IN (SELECT qid FROM labels_staging WHERE review_state IS NOT 'disagree')")
    conn.execute(
        "INSERT OR REPLACE INTO label_facts "
        "SELECT q.q_norm_hash, s.topic, s.qtype, s.difficulty, s.tags, ? "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
        "WHERE COALESCE(s.topic, '') != '' AND s.review_state IS NOT 'disagree'",
        (datetime.now().strftime(NOW_FMT),))
    conn.execute("DELETE FROM labels_staging WHERE review_state IS NOT 'disagree'")
    conn.commit()
    holds = conn.execute("SELECT COUNT(*) FROM labels_staging "
                         "WHERE review_state='disagree'").fetchone()[0]
    out = {"ok": True, "merged": n, "n_review_hold": holds, "gate": g6["state"],
           "next_step": "python -m mianjing_kb.refresh --reembed"
                        "  # 检索库 topic 是快照，merge 只写 kb.sqlite（B2）"}
    if orphans:
        out["dropped_orphans"] = orphans
    if g6b.get("state") == "WARN":
        out["g6b_warn"] = g6b["detail"].get("note", "金标样本不足，未判")
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
    ap.add_argument("--eval", action="store_true",
                    help="金标抽检评估（G6b 准确率/混淆矩阵）→ build/label_eval.json")
    # 批次三 R3（2026-11-19）：复核执行层——LLM 预审 + 人工终审 + 分歧放行
    ap.add_argument("--review", action="store_true",
                    help="LLM 复核三类可疑标签（词表外/低置信/金标弱区）→ review_state")
    ap.add_argument("--review-list", action="store_true",
                    help="打印待人工终审清单（disagree 优先，pending 次之）")
    ap.add_argument("--review-resolve", default="", metavar="SPEC",
                    help='人工终审 "qid:action ..."（keep/suggest/drop），如 "12:keep 33:suggest"')
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
            out.pop("items", None)           # 清单在文件里，控制台只报摘要
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok", True) else 1
        if args.golden_load:
            out = golden_load(conn, Path(args.golden_load), annotator=args.annotator)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        if args.eval:
            g = eval_golden(conn)
            return 0 if g["state"] != "FAIL" else 1
        if args.review:
            out = run_review(cfg, conn, limit=args.limit, budget=args.budget)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0
        if args.review_list:
            rows = review_list(conn)
            for r in rows:
                note = (r['review_note'] or '')[:80]   # 评审 Important#3：打印建议供人工判断
                sug = f" | 建议: {note}" if note else ""
                print(f"  [{r['review_state']:<9}] qid={r['qid']:<6} topic={r['topic']:<10} "
                      f"| {(r['q_text'] or '')[:40]}{sug}")
            print(json.dumps({"listed": len(rows)}, ensure_ascii=False))
            return 0
        if args.review_resolve:
            out = review_resolve(conn, args.review_resolve)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if not out["unknown"] else 1
        if args.merge:
            # merge 会写 questions/label_facts，属于不可重建的事实源 → 先热备份；
            # 并与 intake / doc_convert / review_cli 串行（T15 跨进程写锁）
            try:
                with wlock.write_lock("kb", tool="batch_label",
                                      build_dir=Path(args.db).parent):
                    db.backup_db(args.db)
                    out = merge(conn, drop_orphans=args.drop_orphans)
            except wlock.LockHeld as ex:
                print(json.dumps({"ok": False, "err": str(ex)}, ensure_ascii=False, indent=1))
                return 2
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        out = run_batch(cfg, conn, limit=args.limit, budget=args.budget, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
