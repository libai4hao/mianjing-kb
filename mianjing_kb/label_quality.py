# -*- coding: utf-8 -*-
"""打标批次质量门禁：碎片、pending 未复核、主题漂移。

设计目标：不是把标签分布“做得好看”，而是阻止已知坏样本进入 merge。
- code_fragment > 0：抽取侧已把代码行当题，整批拒绝 merge；
- quality_hold > 0：确定性残缺/答案错配/多题混杂/代码片段尚未人工确认，整批拒绝 merge；
- pending > 0：词表外/非法枚举尚未人工终审，整批拒绝 merge；
- disagree 行已在 staging 单独 hold，不参与门禁，也不阻塞其余行 merge；
- other_topic_rate > 0.25（且批次 >= 20）：主题词表/上下文缺口，整批拒绝 merge；
- other_topic_rate > 0.15：WARN，允许 merge 但必须人工抽查。
- 标签侧两条软告警（2026-11-24 新增，只 WARN 不挡 merge）：
  词表命中率 < 20%（批次 >= 20）、维度重复率 > 2%。前者是词表/prompt 失效的哨兵，
  后者是维度词回流 tags 的回归哨兵（stopwords 生效后应为 0）。
"""
from __future__ import annotations

import json
import sqlite3

from mianjing_kb.chunker import is_code_fragment
from mianjing_kb.kb.labelspec import recommended_tags, tag_synonyms
from mianjing_kb.kb.vocab import OTHER_TOPIC, tag_stopwords
from mianjing_kb.quality_rules import (
    code_fragment_reason,
    deterministic_quality_flags,
    manual_review_flags,
)

# 标签侧 WARN 阈值（软，只告警不挡 merge）——按 2026-11-24 生产实测标定：
# 词表命中率 23.8%（开放词表，低命中是设计如此，判"掉到 20% 以下"= 词表/prompt 失效），
# 维度重复率 0（stopwords 生效后应为 0，>2% 说明 prompt 又开始把 topic/组名塞进 tags）。
VOCAB_HIT_WARN = 0.20
DIM_DUP_WARN = 0.02


def _tag_rates(rows) -> dict | None:
    """用 assess 已取到的行算两个标签侧指标（不另扫一遍 staging）。

    词表命中率 = 归一后落在 `recommended_tags()` 的标签占比；
    维度重复率 = 归一后仍等于 topic/组名/qtype/岗位的标签占比。
    停用词比较不区分大小写（词表是 Zookeeper/Memcached，模型常写 ZooKeeper/memcached）。
    """
    vocab = set(recommended_tags())
    syn = tag_synonyms()
    stop = {str(s).strip().lower() for s in tag_stopwords()}
    total = hit = dup = 0
    for r in rows:
        try:
            raw = r["tags"]
        except (IndexError, KeyError):
            return None                     # 极简单测库没有 tags 列
        try:
            arr = json.loads(raw) if raw else []
        except ValueError:
            arr = []
        if not isinstance(arr, list):
            continue
        for t in arr:
            t = str(t or "").strip()
            if not t:
                continue
            total += 1
            canon = syn.get(t.lower(), t)
            if canon in vocab:
                hit += 1
            if t.lower() in stop or canon.lower() in stop:
                dup += 1
    if total == 0:
        return None
    return {"tags": total, "vocab_hit_rate": round(hit / total, 4),
            "dimension_dup_rate": round(dup / total, 4)}


def assess(conn, *, other_hard: float = 0.25, other_warn: float = 0.15,
           min_batch: int = 20) -> dict:
    try:
        rows = conn.execute(
            "SELECT s.qid, s.topic, s.qtype, s.review_state, s.tags, q.q_text, q.a_text "
            "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
            "WHERE s.review_state IS NOT 'disagree'").fetchall()
    except sqlite3.OperationalError:        # 极简单测库无 tags 列 → 退回不取 tags
        rows = conn.execute(
            "SELECT s.qid, s.topic, s.qtype, s.review_state, q.q_text, q.a_text "
            "FROM labels_staging s JOIN questions q ON q.qid=s.qid "
            "WHERE s.review_state IS NOT 'disagree'").fetchall()
    n = len(rows)
    if n == 0:
        # 全是孤儿行时（questions JOIN 为空）不在质量门禁里判，交给 merge 的孤儿逻辑，
        # 否则 --drop-orphans 会被门禁提前挡住。
        return {"ok": True, "state": "SKIP", "fails": [],
                "detail": {"staged": 0, "note": "无可校验的 staging 行（可能全是孤儿）"}}
    fragments = []
    quality_holds = []
    for r in rows:
        q_text = r["q_text"] or ""
        a_text = r["a_text"] or ""
        code_reason = code_fragment_reason(q_text)
        if is_code_fragment(q_text) or code_reason:
            fragments.append(r["qid"])
        flags = deterministic_quality_flags(q_text, a_text)
        if code_reason:
            flags.add("代码片段")
        manual = manual_review_flags(flags)
        if manual:
            quality_holds.append({"qid": r["qid"], "reasons": sorted(manual)})
    pending = [r["qid"] for r in rows if r["review_state"] == "pending"]
    other = [r["qid"] for r in rows if (r["topic"] or "") == OTHER_TOPIC]
    other_rate = len(other) / n
    fails: list[str] = []
    warns: list[str] = []
    if fragments:
        fails.append(f"code_fragment={len(fragments)}")
    if quality_holds:
        fails.append(f"quality_hold={len(quality_holds)}")
    if pending:
        fails.append(f"pending_review={len(pending)}")
    if n >= min_batch and other_rate > other_hard:
        fails.append(f"other_topic_rate={other_rate:.2%}>{other_hard:.0%}")
    elif other_rate > other_warn:
        warns.append(f"other_topic_rate={other_rate:.2%}>{other_warn:.0%}")
    rates = _tag_rates(rows)
    if rates:
        if n >= min_batch and rates["vocab_hit_rate"] < VOCAB_HIT_WARN:
            warns.append(f"tag_vocab_hit={rates['vocab_hit_rate']:.2%}<{VOCAB_HIT_WARN:.0%}")
        if rates["dimension_dup_rate"] > DIM_DUP_WARN:
            warns.append(f"tag_dimension_dup={rates['dimension_dup_rate']:.2%}"
                         f">{DIM_DUP_WARN:.0%}")
    detail = {
        "staged": n, "code_fragment": len(fragments),
        "quality_hold": len(quality_holds),
        "pending": len(pending), "other_topic": len(other),
        "other_topic_rate": round(other_rate, 4),
        "tag_metrics": rates,
        "fragment_qids": fragments[:10],
        "quality_hold_qids": [x["qid"] for x in quality_holds[:10]],
        "quality_hold_reasons": quality_holds[:10],
        "pending_qids": pending[:10],
        "warns": warns,
    }
    return {
        "ok": not fails,
        "state": "FAIL" if fails else ("WARN" if warns else "PASS"),
        "fails": fails,
        "detail": detail,
    }
