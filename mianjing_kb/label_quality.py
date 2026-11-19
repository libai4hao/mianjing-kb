# -*- coding: utf-8 -*-
"""打标批次质量门禁：碎片、pending 未复核、主题漂移。

设计目标：不是把标签分布“做得好看”，而是阻止已知坏样本进入 merge。
- code_fragment > 0：抽取侧已把代码行当题，整批拒绝 merge；
- pending > 0：词表外/非法枚举尚未人工终审，整批拒绝 merge；
- other_topic_rate > 0.25（且批次 >= 20）：主题词表/上下文缺口，整批拒绝 merge；
- other_topic_rate > 0.15：WARN，允许 merge 但必须人工抽查。
"""
from __future__ import annotations

from mianjing_kb.chunker import is_code_fragment


def assess(conn, *, other_hard: float = 0.25, other_warn: float = 0.15,
           min_batch: int = 20) -> dict:
    rows = conn.execute(
        "SELECT s.qid, s.topic, s.qtype, s.review_state, q.q_text "
        "FROM labels_staging s JOIN questions q ON q.qid=s.qid").fetchall()
    n = len(rows)
    if n == 0:
        return {"ok": False, "state": "SKIP", "fails": [],
                "detail": {"staged": 0, "note": "labels_staging 为空"}}
    fragments = [r["qid"] for r in rows if is_code_fragment(r["q_text"] or "")]
    pending = [r["qid"] for r in rows if r["review_state"] == "pending"]
    other = [r["qid"] for r in rows if (r["topic"] or "") == "其他"]
    other_rate = len(other) / n
    fails: list[str] = []
    warns: list[str] = []
    if fragments:
        fails.append(f"code_fragment={len(fragments)}")
    if pending:
        fails.append(f"pending_review={len(pending)}")
    if n >= min_batch and other_rate > other_hard:
        fails.append(f"other_topic_rate={other_rate:.2%}>{other_hard:.0%}")
    elif other_rate > other_warn:
        warns.append(f"other_topic_rate={other_rate:.2%}>{other_warn:.0%}")
    return {
        "ok": not fails,
        "state": "FAIL" if fails else ("WARN" if warns else "PASS"),
        "fails": fails,
        "detail": {
            "staged": n, "code_fragment": len(fragments),
            "pending": len(pending), "other_topic": len(other),
            "other_topic_rate": round(other_rate, 4),
            "fragment_qids": fragments[:10],
            "pending_qids": pending[:10],
            "warns": warns,
        },
    }
