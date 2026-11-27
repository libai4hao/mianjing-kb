"""补答 pilot（docs/48 §6）：抽 N 条空答案题，用本地模型作答，产出人工复核表。

用法（先设 `KB_CORPUS_ROOT` / `KB_BUILD_DIR`）：
    python scripts/pilot_answer_backfill.py --gap 30 --no-source 20 --model qwen3-8b

产物：`<BUILD_DIR>/pilot-answer-50/answers.jsonl`（机器读）与 `review.md`（人工判定）。
只读库，不写 `questions`；用途是"要不要全量补答"的决策证据，不是入库工具。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mianjing_kb import llm_client  # noqa: E402
from mianjing_kb.kb import config, db as kbdb  # noqa: E402

SYSTEM = (
    "你是 Java/后端面试题答题助手。只输出答案正文，不要复述题目、不要加编号、不要客套。"
    "基于通用知识作答，控制在 120 字以内；若题目本身信息不足或不是一道可作答的题，"
    "只输出一行：NO_ANSWER"
)


def pick(conn, state: str, n: int):
    return conn.execute(
        "SELECT qid, q_text FROM questions WHERE answer_state=? "
        "AND TRIM(COALESCE(a_text,''))='' ORDER BY qid LIMIT ?", (state, n)).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="补答 pilot：抽空答案题让本地模型作答，出人工复核表")
    ap.add_argument("--gap", type=int, default=30, help="boundary_gap 抽几条")
    ap.add_argument("--no-source", type=int, default=20, help="no_answer_source 抽几条")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--out", default="", help="输出目录（默认 <BUILD_DIR>/pilot-answer-50）")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(kbdb.ro_uri(args.db), uri=True)
    rows = [(q, t, "boundary_gap") for q, t in pick(conn, "boundary_gap", args.gap)]
    rows += [(q, t, "no_answer_source") for q, t in pick(conn, "no_answer_source", args.no_source)]
    conn.close()

    cfg = llm_client.resolve_config()
    out = Path(args.out or (Path(args.db).parent / "pilot-answer-50"))
    out.mkdir(parents=True, exist_ok=True)
    recs, t0 = [], time.time()
    for qid, q_text, state in rows:
        try:
            ans = llm_client.chat(cfg, SYSTEM, q_text)["text"]
        except Exception as ex:  # noqa: BLE001 —— 单条失败不拖垮整批
            ans = f"ERROR: {ex}"
        recs.append({"qid": qid, "state": state, "q": q_text, "a": (ans or "").strip()})
    meta = {"n": len(recs), "model": cfg.get("model"),
            "elapsed_sec": round(time.time() - t0, 1),
            "no_answer": sum(1 for r in recs if r["a"] == "NO_ANSWER"),
            "errors": sum(1 for r in recs if r["a"].startswith("ERROR"))}
    with open(out / "answers.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out / "review.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 补答 pilot（人工对照用）\n\n")
        fh.write(f"- 模型 {meta['model']}，耗时 {meta['elapsed_sec']}s，"
                 f"拒答 {meta['no_answer']}/{meta['n']}，失败 {meta['errors']}\n")
        fh.write("- 判定口径：`可用`（可直接背）/`需修`（方向对但有错）/`应拒答`（本题没答案/不是题）\n\n")
        for r in recs:
            fh.write(f"## {r['qid']} [{r['state']}]\n\n**题**：{r['q']}\n\n"
                     f"**模型答**：{r['a']}\n\n判定：\n\n")
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
