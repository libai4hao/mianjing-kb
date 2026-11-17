# -*- coding: utf-8 -*-
"""P3-④ 前置：本地 LLM 冒烟对比（docs/11 §4 的 0.5 天验证）。

对指定模型跑 20 题双任务并量化：
  T1 RAG 问答：rag_contexts top3 → 依据作答；指标 = 延迟、作答长度、grounded 率（答案是否引用 context 词面）
  T2 结构化抽取：题干+答案 → JSON {qtype, difficulty, tags}；指标 = JSON 解析成功率、qtype 准确率
用法：
    python tools/llm_smoke.py --model qwen3:8b
    python tools/llm_smoke.py --model glm4:9b --n 20
输出：build/llm_smoke_<model>.json + 控制台 summary
前提：ollama serve 已在 11434。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config  # noqa: E402
from search import rag_contexts, _con  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
ENDPOINT = "http://localhost:11434/v1/chat/completions"
QTYPES = ["八股", "算法", "HR", "场景", "项目"]
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def chat(model: str, prompt: str, timeout: float = 300.0) -> tuple[str, float]:
    """OpenAI 兼容调用，返回 (回复文本, 延迟秒)。

    Qwen3 默认开思考链，会吃掉大量 token 且污染 JSON 抽取 → 统一追加 /no_think 关闭。
    """
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt + "\n/no_think"}],
                       "stream": False, "temperature": 0.2}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    latency = time.perf_counter() - t0
    text = data["choices"][0]["message"]["content"]
    return THINK_RE.sub("", text).strip(), latency


def sample_questions(n: int, seed: int = 42) -> list[dict]:
    con = _con(OUT_DB)
    rows = con.execute(
        "SELECT q.qid, q.q_text, q.a_text, q.qtype FROM questions q "
        "WHERE q.conf='high' AND length(q.a_text)>100 AND q.a_text NOT LIKE '%```%' "
        "ORDER BY q.qid").fetchall()
    rng = random.Random(seed)
    # 按 qtype 分层抽样，保证覆盖
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["qtype"], []).append(dict(r))
    picked: list[dict] = []
    for qt in QTYPES:
        pool = by_type.get(qt, [])
        picked += rng.sample(pool, min(len(pool), max(1, n // 5)))
    while len(picked) < n and rows:
        cand = rng.choice(rows)
        if cand["qid"] not in {p["qid"] for p in picked}:
            picked.append(dict(cand))
    con.close()
    return picked[:n]


def grounded(answer: str, contexts: list[dict]) -> bool:
    """粗判：答案与 context 有词面交集（去掉停用词后取 2 字以上片段）。"""
    blob = " ".join(c["answer"][:600] for c in contexts)
    for i in range(0, len(answer) - 3, 8):
        frag = answer[i:i + 4]
        if len(frag) >= 3 and frag in blob:
            return True
    return False


def run(model: str, n: int, seed: int) -> dict:
    qs = sample_questions(n, seed)
    con = _con(OUT_DB)
    t1_rows, t2_rows = [], []
    for i, q in enumerate(qs):
        # T1 RAG 问答
        ctx = rag_contexts(con, q["q_text"][:60], 3)
        ctx_txt = "\n\n".join(f"[{j+1}] {c['question']}\n{c['answer'][:400]}"
                              for j, c in enumerate(ctx["contexts"]))
        prompt = (f"根据以下参考资料回答面试题。若资料不足以回答，请明确说\"资料不足\"。\n\n"
                  f"题目：{q['q_text']}\n\n参考资料：\n{ctx_txt}")
        try:
            ans, lat = chat(model, prompt)
            t1_rows.append({"qid": q["qid"], "latency": round(lat, 2),
                            "chars": len(ans), "grounded": grounded(ans, ctx["contexts"]),
                            "answer": ans[:400]})
        except Exception as ex:  # noqa: BLE001
            t1_rows.append({"qid": q["qid"], "err": f"{type(ex).__name__}: {ex}"[:120]})
        # T2 结构化抽取
        prompt2 = ("你是面试题标注器。阅读题目与答案，只输出 JSON，不要解释：\n"
                   '{"qtype": "八股|算法|HR|场景|项目", "difficulty": 1-5整数, "tags": ["关键词", ...]}\n\n'
                   f"题目：{q['q_text'][:300]}\n答案：{q['a_text'][:800]}")
        try:
            out2, lat2 = chat(model, prompt2)
            m = re.search(r"\{.*\}", out2, re.S)
            parsed = json.loads(m.group(0)) if m else None
            ok = parsed is not None and parsed.get("qtype") in QTYPES
            t2_rows.append({"qid": q["qid"], "json_ok": ok,
                            "qtype_correct": bool(ok and parsed.get("qtype") == q["qtype"]),
                            "latency": round(lat2, 2)})
        except Exception as ex:  # noqa: BLE001
            t2_rows.append({"qid": q["qid"], "json_ok": False, "err": f"{type(ex).__name__}"[:60]})
    con.close()

    def _stat(rows, key):
        """latency/chars → 数值均值；bool → 真值率。"""
        vals = [r[key] for r in rows if key in r and isinstance(r[key], (bool, int, float))]
        if not vals:
            return None
        if any(isinstance(v, bool) for v in vals):
            return round(sum(1 for v in vals if v) / len(vals), 3)
        return round(sum(vals) / len(vals), 2)

    summary = {
        "model": model, "n": len(qs), "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "t1_rag": {"err": sum(1 for r in t1_rows if "err" in r),
                   "latency_avg": _stat(t1_rows, "latency"),
                   "grounded_rate": _stat(t1_rows, "grounded"),
                   "answer_chars_avg": _stat([r for r in t1_rows if "chars" in r], "chars")},
        "t2_extract": {"err": sum(1 for r in t2_rows if "err" in r),
                       "latency_avg": _stat(t2_rows, "latency"),
                       "json_ok_rate": _stat(t2_rows, "json_ok"),
                       "qtype_acc": _stat(t2_rows, "qtype_correct")},
    }
    out = {"summary": summary, "t1": t1_rows, "t2": t2_rows}
    outp = Path(config.BUILD_DIR) / f"llm_smoke_{model.replace(':', '_').replace('/', '_')}.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"summary": summary, "out": str(outp)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地 LLM 冒烟对比")
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    res = run(args.model, args.n, args.seed)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
    print("detail:", res["out"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
