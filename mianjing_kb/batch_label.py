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
LLM_PRICE/LLM_JSON_MODE/LLM_ALLOW_CONTENT_UPLOAD）。是否"外发"只看实际 LLM_BASE：
非回环地址（含 local + 远程 base）默认拒绝发送题库文本，需明确设置
LLM_ALLOW_CONTENT_UPLOAD=1；--budget 是软上限，超限后不再提交新请求，
但会收割已在途请求，实际费用最多可能超过一个并发窗口内的请求费用。

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
import inspect
import io
import json
import os
import random
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, labelspec, taxonomy, wlock  # noqa: E402
from mianjing_kb.kb.vocab import (  # noqa: E402
    OTHER_TOPIC,
    tag_stopwords,
    topic_aliases,
    topic_vocab,
)
from mianjing_kb.chunker import is_code_fragment  # noqa: E402
from mianjing_kb.quality_rules import (  # noqa: E402
    code_fragment_reason,
    deterministic_quality_flags,
    manual_review_flags,
)
from mianjing_kb.llm_client import ConfigError, chat, chat_json, redact, resolve_config  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
QTYPES = labelspec.qtypes()          # T21：单一来源（meta/label_schema.yaml）


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """D-28：只读分支的工作文件也原子落盘，避免半截 CSV/JSON 被人工误用。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def build_system_prompt(vocab: tuple[str, ...] = (), tax: dict | None = None) -> str:
    """按 label_schema.yaml + 标签配置表渲染标注 prompt（字段/枚举/上限都不再手写两份）。

    词表非空时锁定 topic 枚举（批次三 R2）；为空时退化为"自由主题"但不改变其它维度。
    `tax` 来自 `kb.taxonomy.load(conn)`（配置表），不传则回退 YAML 预设。
    """
    tax = tax or taxonomy.load()
    diff_lo, diff_hi = labelspec.difficulty_range()
    tag_max, tag_len = labelspec.tag_limits()
    pos = "、".join(labelspec.positions())
    quality = "、".join(labelspec.quality_flags())
    rec = taxonomy.prompt_lines(tax)
    topic_rule = (f"必须从词表中选恰好一个：{'、'.join(vocab)}；"
                  "词表外领域按最接近的选（产品/需求/运营/增长→产品运营，"
                  "HR/求职/自我介绍/职业规划/离职→HR与求职，非技术杂项→非技术）；"
                  "确实无法归类才选其他"
                  if vocab else "2-8 字主题，如 Java基础/MySQL/Redis")
    return (
        "你是面试题标注器。阅读题目与答案片段，只输出 JSON 对象，格式：{"
        f'"topic": <{topic_rule}>, '
        f'"qtype": <从 {"、".join(QTYPES)} 中选恰好一个>, '
        f'"difficulty": <{diff_lo}-{diff_hi} 整数>, '
        f'"position": <岗位，多选，从 {pos} 中选 0~2 个>, '
        f'"quality": <数据质量标记，多选，从 {quality} 中选 0~3 个；没问题给 []>, '
        f'"canonical_q": <标准化问句，≤{labelspec.canonical_max_len()} 字，去口语/去公司名>, '
        f'"tags": <{tag_max} 个以内细粒度关键词，每个≤{tag_len} 字；'
        f'不要重复 topic/题型/岗位这些已有字段，不要近义堆砌；优先二级标签：{rec}>'
        "}"
    )


SYSTEM = build_system_prompt()
# 批次三 R5（bug5）：待打标口径单源——pick_targets 与 health 共用同一谓词，
# 防"未分类算不算待打标"两处漂移（审核报告 bug5）。
TODO_PREDICATE = (f"(q.topic IS NULL OR q.topic = '' "
                  f"OR q.topic = '{config.UNCLASSIFIED}')")


def pick_targets(conn, limit: int = 0, qids: list[int] | None = None,
                 skip_code: bool = True, stats: dict | None = None) -> list[dict]:
    """待打标：题级 topic 为空/未分类的题（口径=TODO_PREDICATE，docs/15 §3.5 裁切口径）。

    优先级：文档级主题弱（空/综合/未分类）的题先打——价值最高（docs/15 §3.5 的
    "先做 4,011 综合级"精神）；文档级主题强的靠后（题级打标是锦上添花）。
    qids 指定时只打这批题（样本集回归用），按 qid 稳定排序。
    skip_code：默认跳过代码行/代码碎片及确定性残缺/错配/多题混杂，防坏样本送进 LLM。
    """
    sql = ("SELECT q.qid, q.q_text, q.a_text, q.q_norm_hash FROM questions q "
           "LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash "
           f"WHERE {TODO_PREDICATE} ")
    if qids:
        sql += "AND q.qid IN (" + ",".join(str(int(x)) for x in qids) + ") "
        sql += "ORDER BY q.qid"
    else:
        sql += ("ORDER BY (sd.topic IS NULL OR sd.topic='' OR sd.topic='综合' "
                "OR sd.topic='未分类') DESC, q.qid")
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    if skip_code:
        skipped, kept = [], []
        for r in rows:
            q_text = r.get("q_text") or ""
            a_text = r.get("a_text") or ""
            code_reason = code_fragment_reason(q_text)
            flags = deterministic_quality_flags(q_text, a_text)
            if code_reason:
                flags.add("代码片段")
            hold_reason = code_reason or ";".join(sorted(manual_review_flags(flags)))
            if is_code_fragment(q_text) or hold_reason:
                skipped.append({**r, "hold_reason": hold_reason or "legacy_code_fragment"})
            else:
                kept.append(r)
        if stats is not None:
            stats["skipped_code"] = len(skipped)
            stats["skipped_code_rows"] = [
                {"qid": r["qid"], "q_text": r["q_text"],
                 "hold_reason": r.get("hold_reason")} for r in skipped]
        rows = kept
    if limit:
        rows = rows[:int(limit)]
    return rows


def label_one(cfg: dict, q: dict, tax: dict | None = None) -> dict:
    """单题打标：脱敏 → LLM → 校验/归一 → staging 行（失败抛异常，调用方落 failed 队列）。

    T21：字段校验交给 `kb.labelspec`（schema 单一来源）——
      - topic 词表外 → "其他" + review_state='pending'（prompt 锁 + 落表双保险）；
      - position/quality 越界 → 丢弃非法值并标 pending；
      - tags 走同义词归一（tag_norm_hits 记录合并数）；
      - canonical_q 为空时回退题干前 60 字。
    T24：`tax` 是标签配置表快照（一级/二级/别名），由 run_batch 一次性加载传入。
    qtype/difficulty 非法直接抛 ValueError（进 failed 队列重试，不污染 staging）。
    """
    tax = tax or taxonomy.load()
    vocab = topic_vocab()
    system = build_system_prompt(vocab, tax)
    text = f"{q['q_text'][:300]}\n{q['a_text'][:300]}"
    clean, hit = redact(text)
    r = chat_json(cfg, system, f"题目与答案片段：\n{clean}", max_tokens=200)
    obj = r.get("obj")
    if obj is None:
        raise RuntimeError("JSON 解析失败")
    spec = labelspec.validate_label(obj, vocab, q_text=q.get("q_text") or "",
                                    synonyms=tax["aliases"],
                                    topic_aliases=topic_aliases(),
                                    tag_stopwords=tag_stopwords())
    return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
            "topic": spec["topic"],
            "qtype": spec["qtype"], "difficulty": spec["difficulty"],
            "position": labelspec.dump_multi(spec["position"]),
            "quality": labelspec.dump_multi(spec["quality"]),
            "canonical_q": spec["canonical_q"],
            "tags": json.dumps(spec["tags"], ensure_ascii=False),
            "tag_norm_hits": spec["tag_norm_hits"],
            "provider": cfg["provider"], "model": cfg["model"],
            "redacted": 1 if hit else 0, "latency_ms": r["latency_ms"],
            "tokens": r["tokens"],
            "prompt_tokens": r.get("prompt_tokens"),
            "completion_tokens": r.get("completion_tokens"),
            "review_state": spec["review_state"],
            # pending 行留"为什么 pending"（含模型原话 topic），人工终审才有依据
            "review_note": (json.dumps({"issues": spec["issues"]}, ensure_ascii=False)
                            if spec.get("issues") else None),
            "labeled_at": datetime.now().strftime(NOW_FMT),
            # 诊断用：原始返回和校验 issues 进 sidecar，不落 staging（区分 prompt/解析/状态机）
            "_raw_response": (r.get("text") or "")[:4000],
            "_issues": spec["issues"]}


def label_one_offline(q: dict, tax: dict | None = None) -> dict:
    """离线规则打标（T24 测试库填充用）：不调 LLM，产出可复核的 staging 行。

    provider='offline'、model='rules-v1'、review_state='pending'——明确标注"非模型结论"，
    只用于测试库/冷启动预标，不能直接当生产打标结果 merge 进正式库。
    """
    import re

    tax = tax or taxonomy.load()
    text = f"{q['q_text'] or ''}\n{q['a_text'] or ''}"
    low = text.lower()
    q_text = re.sub(r"^\s*(\d+[.、)）]|[-*])\s*", "", (q["q_text"] or "").strip())
    qtype = "八股"
    if any(k in text for k in ("手撕", "手写", "实现一个", "写一个", "复杂度", "LeetCode", "算法题")):
        qtype = "算法"
    elif any(k in text for k in ("自我介绍", "职业规划", "离职", "优点", "缺点", "为什么选择")):
        qtype = "HR"
    elif any(k in text for k in ("项目", "难点", "负责")):
        qtype = "项目"
    elif any(k in text for k in ("场景", "如何设计", "怎么办", "线上")):
        qtype = "场景"

    difficulty = 3
    if any(k in text for k in ("原理", "底层", "源码", "为什么", "如何实现")):
        difficulty = 4
    if any(k in text for k in ("区别", "是什么", "介绍一下", "概念")):
        difficulty = 2

    positions: list[str] = []
    if any(k in low for k in ("react", "vue", "前端", "typescript", "浏览器")):
        positions.append("前端")
    if any(k in low for k in ("java", "jvm", "spring", "mysql", "redis", "后端", "并发")):
        positions.append("后端")
    if any(k in text for k in ("算法", "复杂度", "手撕")):
        positions.append("算法")
    positions = positions[:2]

    quality: list[str] = []
    a_text = q["a_text"] or ""
    if len(a_text.strip()) < 20:
        quality.append("残缺")
    if any(k in text for k in ("公众号", "扫码", "加微信", "广告")):
        quality.append("广告")

    vocab = topic_vocab()
    topic = "其他"
    topic_hints = [
        ("Java基础", ("java", "jvm", "gc", "spring", "hashmap", "集合")),
        ("MySQL", ("mysql", "innodb", "索引", "事务")),
        ("Redis", ("redis", "缓存")),
        ("并发编程", ("并发", "线程池", "锁", "synchronized", "aqs")),
        ("计算机网络", ("tcp", "http", "https", "dns", "udp", "网络")),
        ("操作系统", ("进程", "内存", "死锁", "io", "文件系统")),
        ("前端", ("react", "vue", "前端", "typescript", "浏览器")),
    ]
    for name, keys in topic_hints:
        if name in vocab and any(k in low for k in keys):
            topic = name
            break

    tags = [t for t in tax["tags"] if t and t.lower() in low]
    if not tags:                                  # 题干没命中任何二级标签时退回主题名/兜底
        tags = [x for x in (topic,) if x in tax["tags"]] or ["面试题"]
    tags = tags[: labelspec.tag_limits()[0]]
    return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
            "topic": topic, "qtype": qtype, "difficulty": difficulty,
            "position": labelspec.dump_multi(positions),
            "quality": labelspec.dump_multi(quality),
            "canonical_q": q_text[: labelspec.canonical_max_len()],
            "tags": json.dumps(tags, ensure_ascii=False), "tag_norm_hits": 0,
            "provider": "offline", "model": "rules-v1", "redacted": 0,
            "latency_ms": 0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "review_state": "pending", "labeled_at": datetime.now().strftime(NOW_FMT)}


def write_staging(conn, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO labels_staging "
        "(qid, q_norm_hash, topic, qtype, difficulty, tags, position, quality, canonical_q, "
        "tag_norm_hits, provider, model, redacted, latency_ms, tokens, prompt_tokens, "
        "completion_tokens, labeled_at, review_state, review_note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [[r.get(k) for k in ("qid", "q_norm_hash", "topic", "qtype", "difficulty", "tags",
                             "position", "quality", "canonical_q", "tag_norm_hits",
                             "provider", "model", "redacted", "latency_ms", "tokens",
                             "prompt_tokens", "completion_tokens", "labeled_at", "review_state",
                             "review_note")]
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
            w.writerow({k: _csv_safe_cell(row[k]) for k in FAILED_CSV_HEADER})
    return path, len(agg)


def run_batch(cfg: dict, conn, *, limit: int = 0, budget: float = 10.0,
              dry_run: bool = False, failed_path: Path | None = None,
              label_fn=None, qids: list[int] | None = None,
              skip_code: bool = True) -> dict:
    """并发打标 → staging。resume：staging 已有 qid 跳过；budget：估算成本超限即停。

    `label_fn` 可替换单题打标实现（离线规则/手动印入的测试用）；缺省走 LLM。
    标签配置表（taxonomy）在这里一次性加载，避免每题查库。
    """
    stats: dict = {}
    targets = pick_targets(conn, limit, qids=qids, skip_code=skip_code, stats=stats)
    skipped_code = stats.get("skipped_code", 0)
    skipped_rows = stats.get("skipped_code_rows") or []
    staged = {r[0] for r in conn.execute("SELECT qid FROM labels_staging").fetchall()}
    todo = [q for q in targets if q["qid"] not in staged]
    failed_path = failed_path or (Path(config.BUILD_DIR) / "label_failed.jsonl")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path = failed_path.parent / "label_trace.jsonl"
    skipped_path = failed_path.parent / "label_skipped_code.jsonl"
    if skipped_rows:
        with skipped_path.open("w", encoding="utf-8") as fh:
            for r in skipped_rows:
                preview, _ = redact((r["q_text"] or "")[:300])
                fh.write(json.dumps({
                    "qid": r["qid"], "q_text": preview,
                    "reason": "code_fragment",
                    "at": datetime.now().strftime(NOW_FMT),
                }, ensure_ascii=False) + "\n")
    tax = taxonomy.load(conn)
    fn = label_fn or label_one
    try:
        want_tax = "tax" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        want_tax = False

    def call_one(cfg, q):
        return fn(cfg, q, tax) if want_tax else fn(cfg, q)

    done, failed, tokens_total, in_total, out_total = [], [], 0, 0, 0
    issue_counts: dict[str, int] = {}
    started = time.perf_counter()

    def _worker(q):
        try:
            return q, call_one(cfg, q), None
        except Exception as ex:  # noqa: BLE001 —— 单题失败不阻塞整批（docs/15 §3.4）
            return q, None, ex

    # 有界提交：任一批次触达预算后，不再提交新请求；所有已提交请求仍须消费并落 staging。
    # 并发预算是软上限，最坏超支被限制在触发阈值时已在途的请求，不再出现结果丢失。
    max_workers = max(1, int(cfg["concurrency"]))
    pending = {}
    todo_iter = iter(todo)
    submitted = 0
    budget_exceeded = False

    def submit_available(pool):
        nonlocal submitted
        while not budget_exceeded and len(pending) < max_workers:
            try:
                q = next(todo_iter)
            except StopIteration:
                return
            pending[pool.submit(_worker, q)] = q
            submitted += 1

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        submit_available(pool)
        while pending:
            finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for fut in finished:
                pending.pop(fut, None)
                q, row, exc = fut.result()
                if exc is not None:
                    failed.append({"qid": q["qid"], "err_type": type(exc).__name__,
                                   "err": f"{type(exc).__name__}: {str(exc)[:160]}",
                                   "model": cfg["model"],
                                   "at": datetime.now().strftime(NOW_FMT)})
                    with failed_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(failed[-1], ensure_ascii=False) + "\n")
                    continue
                issues = row.pop("_issues", []) or []
                raw = row.pop("_raw_response", "") or ""
                raw, _ = redact(raw)
                for iss in issues:
                    issue_counts[iss] = issue_counts.get(iss, 0) + 1
                with trace_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "qid": row["qid"], "model": row.get("model"),
                        "review_state": row.get("review_state"),
                        "issues": issues, "raw_response": raw,
                        "at": row.get("labeled_at"),
                    }, ensure_ascii=False) + "\n")
                done.append(row)
                tokens_total += row["tokens"]
                in_total += row.get("prompt_tokens") or 0
                out_total += row.get("completion_tokens") or 0
                if not dry_run and len(done) % 20 == 0:
                    write_staging(conn, done[-20:])
                cost = (in_total * cfg["price"][0] + out_total * cfg["price"][1]) / 1e6
                if budget and cost > budget:
                    budget_exceeded = True
            # 即使预算已触发，也继续收割 pending；但不再补充新任务。
            submit_available(pool)
    if done and not dry_run:
        write_staging(conn, done)
    failed_csv, failed_qids = (write_failed_csv(failed_path) if failed else (None, 0))
    return {"todo": len(todo), "labeled": len(done), "failed": len(failed),
            "skipped_code": skipped_code,
            "issues": issue_counts,
            "tokens": tokens_total,
            "est_cost_yuan": round((in_total * cfg["price"][0]
                                    + out_total * cfg["price"][1]) / 1e6, 4),
            "submitted": submitted, "budget_exceeded": budget_exceeded,
            "all_submitted_results_consumed": True,
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


MANUAL_COLUMNS = ["qid", "doc_hash", "q_text", "a_text", "topic", "qtype", "difficulty",
                  "position", "quality", "canonical_q", "tags", "note"]


def manual_template(conn, n: int, path: Path, seed: int = 42) -> dict:
    """生成手动打标模板（CSV，UTF-8 BOM，Excel 直接打开）：抽样 n 题 + 空标签列。

    topic/qtype/difficulty 预填**当前库里的值**（通常为空），人工在此列上改；
    position/quality/tags 多个值用 `|` 分隔。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT qid, doc_hash, q_text, a_text, topic, qtype, difficulty "
        "FROM questions ORDER BY qid").fetchall()]
    # 只抽"能读"的题进模板：乱码/过短/超长的行人工也没法标（生产库实测有乱码题）
    try:
        from mianjing_kb.kb import textmetrics as tm
        rows = [r for r in rows
                if 8 <= len(r["q_text"] or "") <= 200
                and tm.garbled_ratio(r["q_text"] or "") < 0.01
                and (tm.cjk_ratio(r["q_text"] or "") >= 0.3 or (r["q_text"] or "").isascii())]
    except Exception:  # noqa: BLE001 —— 度量不可用时不筛（模板仍可生成）
        pass
    k = min(n, len(rows))
    picked = random.Random(seed).sample(rows, k)
    path = Path(path)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MANUAL_COLUMNS)
    w.writeheader()
    for r in picked:
        w.writerow({k: _csv_safe_cell(v) for k, v in {
                    "qid": r["qid"], "doc_hash": r["doc_hash"] or "",
                    "q_text": (r["q_text"] or "")[:300],
                    "a_text": (r["a_text"] or "")[:300],
                    "topic": r["topic"] or "", "qtype": r["qtype"] or "",
                    "difficulty": r["difficulty"] if r["difficulty"] is not None else "",
                    "position": "", "quality": "", "canonical_q": "", "tags": "", "note": ""
                    }.items()})
    _atomic_write_text(path, buf.getvalue(), encoding="utf-8-sig")
    return {"path": str(path), "rows": k, "columns": MANUAL_COLUMNS}


def _csv_safe_cell(value):
    """防 CSV/Excel 公式注入；以公式字符起始的用户文本在首位加单引号。"""
    if value is None:
        return ""
    text = str(value)
    trimmed = text.lstrip(" \t\r\n")
    if text.startswith(("\t", "\r", "\n")) or trimmed.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _split_multi(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").replace("；", "|").replace(";", "|").split("|")
            if x.strip()]


def manual_import(conn, path: Path, annotator: str = "human", dry_run: bool = False) -> dict:
    """回灌人工填好的 CSV 模板 → labels_staging（逐行校验，坏行单独拒绝并报行号）。

    必填 qid/topic/qtype/difficulty；position/quality/tags 用 `|` 分隔；
    qid 必须存在于 questions（防跨库/跨 extract 残留），canonical_q 空则回退题干。
    """
    p = Path(path)
    if not p.exists():
        return {"ok": False, "errors": [f"文件不存在: {p}"]}
    tax = taxonomy.load(conn)
    vocab = topic_vocab()
    ok_rows, errs = [], []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "qid" not in reader.fieldnames:
            return {"ok": False, "errors": ["模板缺 qid 列（先用 --manual-template 生成）"]}
        for i, row in enumerate(reader, start=2):      # 2 = Excel 行号（1 是表头）
            try:
                qid = int(str(row.get("qid") or "").strip())
            except ValueError:
                errs.append(f"第{i}行: qid 非法 {row.get('qid')!r}")
                continue
            q = conn.execute("SELECT qid, q_norm_hash, q_text, a_text FROM questions WHERE qid=?",
                             (qid,)).fetchone()
            if q is None:
                errs.append(f"第{i}行: qid={qid} 在 questions 不存在")
                continue
            obj = {
                "topic": (row.get("topic") or "").strip(),
                "qtype": (row.get("qtype") or "").strip(),
                "difficulty": (row.get("difficulty") or "").strip(),
                "position": _split_multi(row.get("position")),
                "quality": _split_multi(row.get("quality")),
                "canonical_q": (row.get("canonical_q") or "").strip(),
                "tags": _split_multi(row.get("tags")),
            }
            if not obj["topic"]:
                errs.append(f"第{i}行: topic 为空")
                continue
            try:
                spec = labelspec.validate_label(obj, vocab, q_text=q["q_text"] or "",
                                                synonyms=tax["aliases"],
                                                topic_aliases=topic_aliases(),
                                                tag_stopwords=tag_stopwords())
            except ValueError as ex:
                errs.append(f"第{i}行: {ex}")
                continue
            ok_rows.append({
                "qid": qid, "q_norm_hash": q["q_norm_hash"],
                "topic": spec["topic"], "qtype": spec["qtype"], "difficulty": spec["difficulty"],
                "position": labelspec.dump_multi(spec["position"]),
                "quality": labelspec.dump_multi(spec["quality"]),
                "canonical_q": spec["canonical_q"],
                "tags": json.dumps(spec["tags"], ensure_ascii=False),
                "tag_norm_hits": spec["tag_norm_hits"],
                "provider": "manual", "model": f"human:{annotator}", "redacted": 0,
                "latency_ms": 0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "review_state": spec["review_state"],
                "labeled_at": datetime.now().strftime(NOW_FMT)})
    if ok_rows and not dry_run:
        write_staging(conn, ok_rows)
    return {"ok": True, "imported": len(ok_rows), "rejected": len(errs),
            "errors": errs[:10], "dry_run": dry_run, "path": str(p)}


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
    _atomic_write_text(path, json.dumps({"sampled": len(items), "items": items},
                                        ensure_ascii=False, indent=1))
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
    _atomic_write_text(path, json.dumps(g, ensure_ascii=False, indent=1))
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

    max_workers = max(1, int(cfg["concurrency"]))
    pending = {}
    target_iter = iter(targets)
    submitted = 0
    budget_exceeded = False

    def submit_available(pool):
        nonlocal submitted
        while not budget_exceeded and len(pending) < max_workers:
            try:
                target = next(target_iter)
            except StopIteration:
                return
            pending[pool.submit(_worker, target)] = target
            submitted += 1

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        submit_available(pool)
        while pending:
            finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for fut in finished:
                pending.pop(fut, None)
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
                    budget_exceeded = True
            submit_available(pool)
    # agree/disagree 为全表累计（含此前轮次）；targets/reviewed 是本轮口径（评审 Minor#3）
    agree = conn.execute("SELECT COUNT(*) FROM labels_staging "
                         "WHERE review_state='agree'").fetchone()[0]
    disagree = conn.execute("SELECT COUNT(*) FROM labels_staging "
                            "WHERE review_state='disagree'").fetchone()[0]
    return {"targets": len(targets), "reviewed": done, "failed": len(failed),
            "agree": agree, "disagree": disagree,
            "est_cost_yuan": round((in_total * cfg["price"][0]
                                    + out_total * cfg["price"][1]) / 1e6, 4),
            "submitted": submitted, "budget_exceeded": budget_exceeded,
            "all_submitted_results_consumed": True,
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


def _propagate_by_hash(conn, hashes) -> dict:
    """同一 q_norm_hash 的多份拷贝（同题出现在多篇源文档）落库后统一 topic。

    起因（docs/51 §5）：拷贝是逐 qid 独立打标的，4,556 组拷贝里 934 处 topic 互相矛盾，
    检索侧同一道题出现两种主题，主题过滤会漏。merge 时把该 hash 的多数值推给所有拷贝，
    平票不猜；顺带把空白拷贝的 tags/qtype/difficulty 从有值的兄弟行补齐（等于免费覆盖待打标）。
    """
    from collections import Counter

    changed = filled = 0
    for h in hashes:
        rows = conn.execute("SELECT qid, topic, qtype, difficulty, tags FROM questions "
                            "WHERE q_norm_hash=?", (h,)).fetchall()
        if len(rows) < 2:
            continue
        counts = Counter(r["topic"] for r in rows
                         if r["topic"] and r["topic"] not in ("", OTHER_TOPIC))
        if not counts:
            continue
        top, n = counts.most_common(1)[0]
        if any(v >= n for t, v in counts.items() if t != top):   # 平票不在门禁里猜
            continue
        src = next((r for r in rows if r["topic"] == top and r["tags"]), None)
        for r in rows:
            if r["topic"] != top:
                conn.execute("UPDATE questions SET topic=? WHERE qid=?", (top, r["qid"]))
                changed += 1
            if src is not None and not r["tags"]:
                conn.execute(
                    "UPDATE questions SET tags=?, qtype=COALESCE(NULLIF(qtype,''), ?), "
                    "difficulty=COALESCE(difficulty, ?) WHERE qid=?",
                    (src["tags"], src["qtype"], src["difficulty"], r["qid"]))
                filled += 1
    return {"hash_rows_changed": changed, "hash_rows_filled": filled}


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
    - 2026-11-20 质量门禁：code_fragment>0 / pending>0 / 其他主题率>25% 直接拒绝
      merge（先跑 label_quality.assess，pending 不再被静默合并）。
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
    from mianjing_kb.label_quality import assess as quality_assess
    quality = quality_assess(conn)
    if not quality["ok"]:
        return {"ok": False, "gate": "LABEL_QUALITY", "detail": quality["detail"],
                "fails": quality["fails"],
                "note": "批次质量门禁未过：碎片/待复核/其他主题超限，staging 保留未 merge——"
                        "先清理碎片、--review-resolve 处理 pending，或修词表后重跑"}
    quality_warn = quality["detail"].get("warns") if quality["state"] == "WARN" else []
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
                     "WHERE review_state IS NOT 'disagree' "
                     "  AND review_state IS NOT 'pending'").fetchone()[0]
    conn.execute(
        "UPDATE questions SET "
        " topic=COALESCE(NULLIF((SELECT s.topic FROM labels_staging s "
        "                        WHERE s.qid=questions.qid), ''), topic), "
        " qtype=COALESCE((SELECT s.qtype FROM labels_staging s WHERE s.qid=questions.qid), qtype), "
        " difficulty=COALESCE((SELECT s.difficulty FROM labels_staging s WHERE s.qid=questions.qid), "
        "difficulty), "
        " tags=COALESCE(NULLIF((SELECT s.tags FROM labels_staging s WHERE s.qid=questions.qid), "
        "'[]'), tags), "
        " position=COALESCE((SELECT s.position FROM labels_staging s WHERE s.qid=questions.qid), "
        "position), "
        " quality=COALESCE((SELECT s.quality FROM labels_staging s WHERE s.qid=questions.qid), "
        "quality), "
        " canonical_q=COALESCE(NULLIF((SELECT s.canonical_q FROM labels_staging s "
        "                             WHERE s.qid=questions.qid), ''), canonical_q) "
        "WHERE qid IN (SELECT qid FROM labels_staging "
        "              WHERE review_state IS NOT 'disagree' "
        "                AND review_state IS NOT 'pending')")
    conn.execute(
        "INSERT OR REPLACE INTO label_facts "
        "(q_norm_hash, topic, qtype, difficulty, tags, normalizer_version, q_norm_sample, "
        " source, labeled_at, confidence, merged_at) "
        "SELECT q.q_norm_hash, s.topic, s.qtype, s.difficulty, s.tags, ?, q.q_norm, "
        "       'model', s.labeled_at, NULL, ? "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
        "WHERE COALESCE(s.topic, '') != '' AND s.review_state IS NOT 'disagree' "
        "  AND s.review_state IS NOT 'pending'",
        (config.NORMALIZER_VERSION, datetime.now().strftime(NOW_FMT)))
    merged_hashes = [r[0] for r in conn.execute(
        "SELECT DISTINCT q_norm_hash FROM labels_staging "
        "WHERE q_norm_hash IS NOT NULL AND q_norm_hash<>'' "
        "  AND review_state IS NOT 'disagree' AND review_state IS NOT 'pending'")]
    consistency = _propagate_by_hash(conn, merged_hashes)
    conn.execute("DELETE FROM labels_staging WHERE review_state IS NOT 'disagree' "
                 "AND review_state IS NOT 'pending'")
    conn.commit()
    holds = conn.execute("SELECT COUNT(*) FROM labels_staging "
                         "WHERE review_state IN ('disagree','pending')").fetchone()[0]
    out = {"ok": True, "merged": n, "n_review_hold": holds, "gate": g6["state"],
           "hash_consistency": consistency,
           "next_step": "python -m mianjing_kb.refresh --reembed"
                        "  # 检索库 topic 是快照，merge 只写 kb.sqlite（B2）"}
    if orphans:
        out["dropped_orphans"] = orphans
    if g6b.get("state") == "WARN":
        out["g6b_warn"] = g6b["detail"].get("note", "金标样本不足，未判")
    if quality_warn:
        out["quality_warn"] = quality_warn
    if g6["state"] == "WARN":
        out["note"] = "topic 漂移 WARN（软门禁不阻断）——建议 --sample 50 人工抽查确认"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LLM 批量打标（staging + G6 门禁 + merge）")
    ap.add_argument("--check", action="store_true", help="health：配置/连通/待打标数/预算估算")
    ap.add_argument("--dry-run", action="store_true", help="打标但不写 staging（试输出）")
    ap.add_argument("--limit", type=int, default=0, help="最多打标 N 题（试跑）")
    ap.add_argument("--qids-file", default="",
                    help="只打指定 qid 清单（每行一个；样本集回归用）")
    ap.add_argument("--include-code-fragments", action="store_true",
                    help="诊断用：不跳过代码行/代码碎片（默认跳过，防碎片进正常标注）")
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
    # T24：离线规则打标 + 手动模板/回灌（不需要 LLM 配置，先于 resolve_config 处理）
    ap.add_argument("--offline", action="store_true",
                    help="离线规则打标（provider=offline，仅测试库/冷启动预标；不花钱）")
    ap.add_argument("--manual-template", type=int, default=0, metavar="N",
                    help="抽样 N 题生成手动打标 CSV 模板 → --manual-out（默认 db 同目录 manual_label_template.csv）")
    ap.add_argument("--manual-out", default="", help="模板输出路径")
    ap.add_argument("--manual-import", default="", metavar="PATH",
                    help="回灌人工填好的 CSV 模板 → labels_staging（逐行校验、坏行报行号）")
    ap.add_argument("--manual-dry-run", action="store_true", help="配合 --manual-import：只校验不写库")
    args = ap.parse_args(argv)

    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.concurrency:
        os.environ["LLM_CONCURRENCY"] = str(args.concurrency)

    # D-28：先把“只读分支”和“写库分支”分开。只读分支用 mode=ro 打开，
    # 不持 kb 写锁；需要写库/写 staging 的分支才在 db.connect 之前拿写锁。
    write_action = bool(
        (args.manual_import and not args.manual_dry_run)
        or args.offline or args.golden_load or args.review
        or args.review_resolve or args.merge
    )
    read_action = bool(
        args.check or args.sample or args.eval or args.review_list
        or args.golden_sample or args.manual_template
        or (args.manual_import and args.manual_dry_run) or args.dry_run
    )
    needs_write = write_action or not read_action

    lock = None
    if needs_write:
        lock = wlock.write_lock("kb", tool="batch_label", build_dir=Path(args.db).parent,
                                target=args.db)
        try:
            lock.__enter__()
        except wlock.LockHeld as ex:
            print(json.dumps({"ok": False, "err": str(ex)}, ensure_ascii=False, indent=1))
            return 2

    conn = None
    try:
        # 只读分支绝不 create=True：避免 --check/--sample 顺手建库或跑迁移。
        try:
            conn = db.connect(args.db, readonly=not needs_write)
        except FileNotFoundError as ex:
            print(json.dumps({"ok": False, "err": str(ex)}, ensure_ascii=False, indent=1))
            return 2
        if args.manual_template:
            out_path = Path(args.manual_out) if args.manual_out else (
                Path(args.db).parent / "manual_label_template.csv")
            print(json.dumps(manual_template(conn, args.manual_template, out_path),
                             ensure_ascii=False, indent=1))
            return 0
        if args.manual_import:
            out = manual_import(conn, Path(args.manual_import), annotator=args.annotator,
                                dry_run=args.manual_dry_run)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok") else 1
        if args.offline:
            cfg = {"provider": "offline", "model": "rules-v1", "base": "", "key": "",
                   "concurrency": 4, "timeout": 1, "json_mode": False, "price": (0.0, 0.0)}
            tax = taxonomy.load(conn)
            out = run_batch(cfg, conn, limit=args.limit, budget=0.0,
                            label_fn=lambda c, q: label_one_offline(q, tax))
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0

        try:
            cfg = resolve_config()
        except ConfigError as ex:
            print(json.dumps({"fatal": str(ex)}, ensure_ascii=False, indent=1))
            return 2

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
            # 已经在 main 入口持有 kb 写锁，这里不再重复加锁。
            db.backup_db(args.db)
            out = merge(conn, drop_orphans=args.drop_orphans)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["ok"] else 1
        qids = None
        if args.qids_file:
            p = Path(args.qids_file)
            if not p.exists():
                print(json.dumps({"ok": False, "error": f"qids 文件不存在: {p}"},
                                 ensure_ascii=False, indent=1))
                return 2
            raw = p.read_text(encoding="utf-8")
            try:
                qids = [int(x) for x in raw.replace(",", " ").split()]
            except ValueError:
                print(json.dumps({"ok": False, "error": "qids 文件必须是整数 qid，每行一个"},
                                 ensure_ascii=False, indent=1))
                return 2
            if not qids:
                print(json.dumps({"ok": False, "error": "qids 文件为空"},
                                 ensure_ascii=False, indent=1))
                return 2
        out = run_batch(cfg, conn, limit=args.limit, budget=args.budget,
                        dry_run=args.dry_run, qids=qids,
                        skip_code=not args.include_code_fragments)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            lock.__exit__(None, None, None)


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
