# -*- coding: utf-8 -*-
"""qcheck：固定种子抽样集 —— 建一次样本，之后每次改动都用同一批题快速校验。

为什么要有它
------------
`docs/43` 的 55 题抽检是一次性的：样本随机抽完就没了，下轮改动无法对比。固定样本集可以让
"改前 / 改后"落在同一批题上，只看标签变化，省掉重复抽样和口径漂移。

样本按 `questions.answer_state` 分层（018 迁移的标记）：
- `ok`               有答案题，是准确率的主分母；
- `boundary_gap`     疑似切块缺陷，重点盯修复效果；
- `no_answer_source` 源文档没答案，算"口径外"（不该拿它扣准确率，但要看占比）；
- `toc_noise`        目录噪声，看退役进展。

用法
----
    python -m mianjing_kb.qcheck make --db <kb.sqlite> --n 200 --seed 20261123 \
        --out eval/qcheck/set-20261123.json
    python -m mianjing_kb.qcheck render --db <kb.sqlite> --set <set.json> --out <dir>
    python -m mianjing_kb.qcheck stats --set <set.json> --labels <labels.json>

`labels.json` 形如 `{"<qid>": {"label": "pass|partial|fail", "reason": "..."}}`；
未标注的题在统计里记 `unlabeled`，不混进准确率。
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config  # noqa: E402

STATES = ("ok", "boundary_gap", "no_answer_source", "toc_noise")
LABELS = ("pass", "partial", "fail")
DEFAULT_QUOTAS = {"ok": 120, "boundary_gap": 40, "no_answer_source": 30, "toc_noise": 10}


def _rows(conn) -> list[dict]:
    docs = {r[0]: r[1] for r in conn.execute(
        "select doc_hash, file_name from source_documents")}
    out = []
    for qid, dh, qt, q, a, st in conn.execute(
        "select qid, doc_hash, qtype, q_text, coalesce(a_text,''), coalesce(answer_state,'') "
        "from questions"
    ):
        out.append({"qid": qid, "doc_hash": dh, "qtype": qt,
                    "doc": docs.get(dh, ""), "answer_state": st,
                    "q": q, "a": a})
    return out


def make_set(rows: list[dict], n: int = 200, seed: int = 20261123,
             quotas: dict | None = None) -> dict:
    """按 answer_state 分层抽样；总数按 `n` 等比缩放配额。"""
    quotas = dict(quotas or DEFAULT_QUOTAS)
    total_quota = sum(quotas.values())
    if n != total_quota:
        factor = n / total_quota
        quotas = {k: max(1, round(v * factor)) for k, v in quotas.items()}
    rnd = random.Random(seed)
    by_state: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_state[r["answer_state"] or "unmarked"].append(r)
    picked = []
    for st, want in quotas.items():
        pool = sorted(by_state.get(st, []), key=lambda x: x["qid"])
        take = min(want, len(pool))
        picked += rnd.sample(pool, take)
    picked.sort(key=lambda x: (x["answer_state"], x["qid"]))
    return {
        "seed": seed,
        "n": len(picked),
        "quotas_requested": quotas,
        "pool_sizes": {k: len(v) for k, v in by_state.items()},
        "items": [{"qid": p["qid"], "doc_hash": p["doc_hash"], "qtype": p["qtype"],
                   "doc": p["doc"], "answer_state": p["answer_state"],
                   "label": None, "reason": ""} for p in picked],
    }


def render(conn, items: list[dict]) -> tuple[str, list[dict]]:
    """把样本渲染成可通读的 md + 明细 json（含题干/答案/来源文档）。"""
    detail = []
    for it in items:
        row = conn.execute(
            "select q_text, coalesce(a_text,''), qtype, doc_hash from questions where qid=?",
            (it["qid"],),
        ).fetchone()
        if not row:
            continue
        doc = conn.execute(
            "select file_name from source_documents where doc_hash=?", (row[3],)
        ).fetchone()
        detail.append({"qid": it["qid"], "q": row[0], "a": row[1], "qtype": row[2],
                       "doc": doc[0] if doc else "", "answer_state": it.get("answer_state"),
                       "label": it.get("label"), "reason": it.get("reason", "")})
    lines = ["# 固定抽样校验集", "", f"共 {len(detail)} 题", ""]
    for i, d in enumerate(detail, 1):
        lines += [f"## {i}. [{d['qid']}] {d['qtype']} · {d['answer_state']} · {d['doc'][:60]}",
                  "", f"**Q**：{d['q']}", "", f"**A**：{d['a'] or '（空）'}", ""]
    return "\n".join(lines), detail


def stats(items: list[dict], labels: dict) -> dict:
    """算三档分布、严格准确率/可用率，并按 answer_state 分层。

    - 严格准确率 = pass / (pass+partial+fail)
    - 可用率     = (pass+partial) / (pass+partial+fail)
    - `no_answer_source` 另列"口径外"：它答不出来不是切块缺陷，但会影响用户体验。
    """
    counts = Counter()
    per_state = defaultdict(Counter)
    unlabeled = 0
    for it in items:
        lab = (labels.get(str(it["qid"])) or {}).get("label") or it.get("label")
        st = it.get("answer_state") or "unmarked"
        if lab not in LABELS:
            unlabeled += 1
            per_state[st]["unlabeled"] += 1
            continue
        counts[lab] += 1
        per_state[st][lab] += 1
    judged = sum(counts[l] for l in LABELS)
    out = {
        "items": len(items),
        "judged": judged,
        "unlabeled": unlabeled,
        "counts": {l: counts.get(l, 0) for l in LABELS},
        "strict_accuracy": round(counts["pass"] / judged, 4) if judged else None,
        "usable_rate": round((counts["pass"] + counts["partial"]) / judged, 4) if judged else None,
        "by_state": {st: dict(per_state[st]) for st in sorted(per_state)},
    }
    # 有答案口径：把"源文档没答案"排除在分母外，用于衡量切块/抽取质量本身
    answered = Counter()
    for st in ("ok", "boundary_gap", "toc_noise"):
        answered.update(per_state[st])
    aj = sum(answered.get(l, 0) for l in LABELS)
    out["answered_scope"] = {
        "judged": aj,
        "strict_accuracy": round(answered.get("pass", 0) / aj, 4) if aj else None,
        "usable_rate": round((answered.get("pass", 0) + answered.get("partial", 0)) / aj, 4)
        if aj else None,
        "states": ["ok", "boundary_gap", "toc_noise"],
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="固定抽样校验集（make / render / stats）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("make", help="生成固定样本集")
    m.add_argument("--db", default=str(config.DB_PATH))
    m.add_argument("--n", type=int, default=200)
    m.add_argument("--seed", type=int, default=20261123)
    m.add_argument("--out", required=True)
    m.add_argument("--carry", default=None,
                   help="旧抽检 result.json：把已判定过的 qid 与标签带进新样本（保连续性）")

    r = sub.add_parser("render", help="按样本集导出 md + json（人工通读用）")
    r.add_argument("--db", default=str(config.DB_PATH))
    r.add_argument("--set", required=True)
    r.add_argument("--out", required=True)

    s = sub.add_parser("stats", help="按标注算准确率（分层）")
    s.add_argument("--set", required=True)
    s.add_argument("--labels", default=None)
    s.add_argument("--out", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "make":
        conn = sqlite3.connect(str(Path(args.db)))
        conn.row_factory = sqlite3.Row
        rows = _rows(conn)
        payload = make_set(rows, n=args.n, seed=args.seed)
        if args.carry:
            carried = json.loads(Path(args.carry).read_text(encoding="utf-8"))
            labels = {str(x["qid"]): {"label": x.get("label"), "reason": x.get("reason", "")}
                      for x in carried.get("items", []) if x.get("label")}
            by_qid = {r["qid"]: r for r in rows}
            have = {it["qid"] for it in payload["items"]}
            for qid, lab in labels.items():
                q = int(qid)
                if q in have or q not in by_qid:
                    continue
                r = by_qid[q]
                payload["items"].append({
                    "qid": q, "doc_hash": r["doc_hash"], "qtype": r["qtype"],
                    "doc": r["doc"], "answer_state": r["answer_state"],
                    "label": lab["label"], "reason": lab["reason"]})
            payload["carried"] = len(payload["items"]) - payload["n"]
            payload["items"].sort(key=lambda x: (x["answer_state"], x["qid"]))
            payload["n"] = len(payload["items"])
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({"n": payload["n"], "quotas": payload["quotas_requested"],
                          "pool_sizes": payload["pool_sizes"], "out": str(out)},
                         ensure_ascii=False, indent=1))
        return 0

    payload = json.loads(Path(args.set).read_text(encoding="utf-8"))
    if args.cmd == "render":
        conn = sqlite3.connect(str(Path(args.db)))
        conn.row_factory = sqlite3.Row
        md, detail = render(conn, payload["items"])
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "sample.md").write_text(md, encoding="utf-8")
        (out / "sample.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
        print(json.dumps({"rendered": len(detail), "out": str(out)}, ensure_ascii=False))
        return 0

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) if args.labels else {}
    res = stats(payload["items"], labels)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
