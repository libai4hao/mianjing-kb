# -*- coding: utf-8 -*-
"""qsalvage：空答案题（a_text 为空）的确定性回收与退役。

背景（docs/43 抽检）：19,913 题里 5,300 题（26.6%）答案为空，直接拉低可用率。
按成因拆开看，只有两类可以靠代码确定性处理，其余是源文档本身就没答案：

R1 题答同行合并 —— docx/PDF 无换行时，切题器把「…？答：…」整行当题干，答案留在
   题干里。`split_inline_answer()` 从「答：」标记处拆回 q/a，答案立即可用。
R2 目录行 —— 点线 + 页码的目录条目（`服务续约 ...... 54`）被当成题。这类不是题，
   `toc_row_reason()` 判为目录行，从题库退役（写 retire CSV 留痕）。
R3 目录区/清单区 —— 连续多块空答案，靠 `toc_runs()` 只出复核清单，**不自动删**：
   「面试真题目录.docx」这类文档的条目是真的面试题，只是原文没给答案，删了是丢内容。
R4 其余空答案 —— `answer_missing` 导出 qid + 归因，供后续本地 LLM 补答与统计分层。

口径：本模块只按文本结构判定，不猜语义；每条改动都落 CSV/JSON 便于复核与回滚。
用法：
    python -m mianjing_kb.qsalvage --db <kb.sqlite>              # 预览
    python -m mianjing_kb.qsalvage --db <kb.sqlite> --apply      # 执行（走 kb 写锁）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, wlock  # noqa: E402
from mianjing_kb.kb.qtext import clean_question_text  # noqa: E402
from mianjing_kb.question_freq import normalize, simhash  # noqa: E402
from mianjing_kb.extract import _signed64  # noqa: E402

MIN_NORM = 4
NOW_FMT = "%Y-%m-%dT%H:%M:%S"

# 「答：」「答案:」等题答分隔标记（必须紧跟在前一字符之后，不能在行首）
_ANS_MARK = re.compile(r"(?P<pre>[\s。？?！!、，,）)])?(?:参考)?答\s*案?\s*[:：]")
# 点线 + 页码：目录条目的稳定特征（`服务续约 ...... 54`、`微服务...4`）
_DOT_PAGE = re.compile(r"[.．·…]{2,}\s*[0-9０-９]{1,4}\s*$")
_ASK = re.compile(
    r"[?？]|什么|如何|为什么|区别|怎么|哪些|是否|能否|讲讲|谈谈|说说|介绍|解释|"
    r"实现|设计|举例|比较|说一下|写一个|几个|多少"
)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def split_inline_answer(q_text: str | None) -> tuple[str, str] | None:
    """把「问题…答：答案」拆成 (问题, 答案)。

    只认行内「答：/答案：」标记，且要求：标记前是问句形态（含问号或问词）、
    标记后至少 8 字。返回 None 表示不是题答合并行。
    """
    s = str(q_text or "").strip()
    if len(s) < 12:
        return None
    m = _ANS_MARK.search(s)
    if not m:
        return None
    head = s[: m.start()].strip()
    pre = (m.group("pre") or "").strip()
    if pre and pre in "。？?！!":
        head = head + pre          # 问号/句号被匹配吃掉，还回题干尾
    tail = s[m.end():].strip()
    if len(head) < 6 or len(tail) < 8:
        return None
    if not _ASK.search(head):
        return None
    return head, tail[:20000]


def toc_row_reason(q_text: str | None) -> str | None:
    """目录行判定：点线 + 行尾页码。返回原因或 None。

    要求全行 ≤60 字（目录条目短），避免把正文长句里的省略号误判成目录。
    """
    s = str(q_text or "").strip()
    if not s or len(s) > 60:
        return None
    if not _DOT_PAGE.search(s):
        return None
    # 目录条目里也可能有问号（「Spring 原理」无，但「如何...」有），仍按目录处理：
    # 点线+页码是排版特征，与是否问句无关。
    return "dot_leader_page"


def toc_runs(rows: list[tuple], min_len: int = 8, title_ratio: float = 0.8,
             max_len: int = 40) -> list[list[tuple]]:
    """连续空答案串中「标题式」占多数的段（目录区/清单区候选）。

    rows: [(seq, q_text, a_text)]，同一文档按 seq 升序。返回 run 列表，每段是 rows 切片。
    仅用于出具复核清单，不做自动删除。
    """
    out, run = [], []

    def flush():
        if len(run) < min_len:
            run.clear()
            return
        title_like = sum(1 for r in run if not _ASK.search(r[1]) and len(r[1]) <= max_len)
        if title_like / len(run) >= title_ratio:
            out.append(list(run))
        run.clear()

    for r in rows:
        if not (r[2] or "").strip():
            run.append(r)
        else:
            flush()
    flush()
    return out


def _identity(q_text: str) -> dict:
    """按 extract 同口径重算题目身份字段。"""
    qn = normalize(q_text)
    q_norm_hash = hashlib.sha256(qn.encode("utf-8")).hexdigest()[:16]
    return {
        "q_text": clean_question_text(q_text)[:2000],
        "q_norm": qn[:500],
        "q_norm_hash": q_norm_hash,
        "simhash": _signed64(simhash(qn)),
    }


def blank_whitespace_answers(conn) -> int:
    """把「只含空白」的 a_text 归一成空串。

    SQLite 的 `trim(x)` 默认只去空格，不去换行；`a_text='\\n\\n'` 会被 SQL 口径算成
    "有答案"，Python `.strip()` 口径算成"空答案"，两个口径对不上（docs/44 记了这次差 20 条）。
    questions.a_text 是 NOT NULL，所以归一到 ''（不是 NULL），让两种统计一致。
    """
    return conn.execute(
        "update questions set a_text='' "
        "where a_text is not null and trim(a_text, char(9)||char(10)||char(13)||char(32))=''"
    ).rowcount


def plan(conn, run_min_len: int = 8) -> dict:
    """只读：算出待拆、待退役、待补答与待复核清单。"""
    from collections import defaultdict

    rows = conn.execute(
        "select qid, doc_hash, seq, q_text, coalesce(a_text,''), q_key, qtype "
        "from questions order by doc_hash, seq"
    ).fetchall()
    keys_by_doc = defaultdict(set)
    for r in rows:
        keys_by_doc[r[1]].add(r[5])

    split, retire = [], []
    for qid, dh, seq, q, a, qkey, qt in rows:
        if a.strip():
            continue
        cand = split_inline_answer(q)
        if cand:
            new_q, new_a = cand
            ident = _identity(new_q)
            new_key = f"{dh}:{ident['q_norm_hash']}"
            split.append({
                "qid": qid, "doc_hash": dh, "seq": seq, "qtype": qt, "old_q_key": qkey,
                "old_q": q[:160], "new_q": ident["q_text"][:160], "a_len": len(new_a),
                "new_q_key": new_key,
                "conflict": new_key != qkey and new_key in keys_by_doc[dh],
                "new_a": new_a,
            })
            keys_by_doc[dh].add(new_key)
            continue
        reason = toc_row_reason(q)
        if reason:
            retire.append({"qid": qid, "doc_hash": dh, "seq": seq, "qtype": qt,
                           "q_key": qkey, "reason": reason, "q": q[:160]})

    missing = [{"qid": r[0], "doc_hash": r[1], "qtype": r[6], "q": (r[3] or "")[:120]}
               for r in rows if not r[4].strip()]

    per_doc = defaultdict(list)
    for qid, dh, seq, q, a, qkey, qt in rows:
        per_doc[dh].append((seq, (q or "").strip(), a))
    runs = []
    for dh, rs in per_doc.items():
        for run in toc_runs(sorted(rs), min_len=run_min_len):
            runs.append({"doc_hash": dh, "n": len(run),
                         "qids": [x[0] for x in run], "first": [x[1][:50] for x in run[:3]]})

    return {
        "before": {"questions": len(rows), "empty": len(missing)},
        "split": split,
        "retire": retire,
        "runs_review": sorted(runs, key=lambda x: -x["n"]),
        "answer_missing": missing,
    }


def apply(conn, p: dict, out_dir: Path, write_missing: bool = True) -> dict:
    """执行拆分与退役。调用方负责事务与写锁。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    touched_canonical = set()
    applied_split = applied_retire = skipped = 0
    blanked = blank_whitespace_answers(conn)

    for s in p["split"]:
        if s["conflict"]:
            skipped += 1
            continue
        ident = _identity(s["new_q"])
        conn.execute(
            "update questions set q_text=?, a_text=?, q_norm=?, q_norm_hash=?, simhash=?, q_key=? "
            "where qid=?",
            (ident["q_text"], s["new_a"], ident["q_norm"], ident["q_norm_hash"],
             ident["simhash"], s["new_q_key"], s["qid"]),
        )
        # 已打标事实/簇依赖按 q_key 记录，跟着改；answering_text 同步成新答案。
        n1 = conn.execute(
            "update canonical_dependents set q_key=? where q_key=?",
            (s["new_q_key"], s["old_q_key"]),
        ).rowcount
        n2 = conn.execute(
            "update question_variants set q_key=?, answering_text=? where q_key=?",
            (s["new_q_key"], s["new_a"], s["old_q_key"]),
        ).rowcount
        if n1 or n2:
            touched_canonical.add(s["old_q_key"])
        applied_split += 1

    for r in p["retire"]:
        conn.execute("delete from canonical_dependents where q_key=?", (r["q_key"],))
        conn.execute("delete from question_variants where q_key=?", (r["q_key"],))
        conn.execute("delete from questions where qid=?", (r["qid"],))
        applied_retire += 1

    cq_removed = 0
    if applied_retire or touched_canonical:
        before = conn.execute("select count(*) from canonical_questions").fetchone()[0]
        conn.execute("delete from canonical_questions where canonical_id not in "
                     "(select distinct canonical_id from canonical_dependents)")
        cq_removed = before - conn.execute("select count(*) from canonical_questions").fetchone()[0]

    with open(out_dir / "retire.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["qid", "doc_hash", "seq", "qtype", "reason", "q"],
                           extrasaction="ignore")
        w.writeheader()
        for r in p["retire"]:
            w.writerow(r)
    with open(out_dir / "split.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["qid", "doc_hash", "qtype", "old_q", "new_q", "a_len"],
                           extrasaction="ignore")
        w.writeheader()
        for s in p["split"]:
            w.writerow(s)
    if write_missing:
        with open(out_dir / "answer_missing.jsonl", "w", encoding="utf-8") as f:
            for m in p["answer_missing"]:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(out_dir / "runs_review.json", "w", encoding="utf-8") as f:
        json.dump(p["runs_review"], f, ensure_ascii=False, indent=1)

    return {"split_applied": applied_split, "split_skipped_conflict": skipped,
            "retired": applied_retire, "canonical_questions_removed": cq_removed,
            "blanked_whitespace_a_text": blanked}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="空答案题回收（题答拆分 / 目录行退役 / 补答清单）")
    ap.add_argument("--db", default=str(config.DB_PATH), help="目标 kb.sqlite")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--out", default=None, help="产物目录（默认 <BUILD_DIR>/qsalvage）")
    ap.add_argument("--run-min-len", type=int, default=8, help="目录区复核清单的最小连续长度")
    args = ap.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(json.dumps({"error": f"数据库不存在：{path}"}, ensure_ascii=False))
        return 2
    out_dir = Path(args.out) if args.out else config.BUILD_DIR / "qsalvage"
    conn = __import__("sqlite3").connect(str(path))
    conn.row_factory = __import__("sqlite3").Row
    try:
        p = plan(conn, run_min_len=args.run_min_len)
        head = {
            "before": p["before"],
            "split": len(p["split"]),
            "split_conflict": sum(1 for s in p["split"] if s["conflict"]),
            "retire": len(p["retire"]),
            "runs_review": len(p["runs_review"]),
            "runs_review_empty": sum(r["n"] for r in p["runs_review"]),
            "answer_missing": len(p["answer_missing"]),
            "apply": bool(args.apply),
        }
        if args.apply:
            lock_name = "kb"
            with wlock.write_lock(lock_name, tool="qsalvage", build_dir=config.BUILD_DIR,
                                  wait_sec=30.0, target=path):
                with conn:
                    head.update(apply(conn, p, out_dir))
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / "plan.json", "w", encoding="utf-8") as f:
                json.dump({k: p[k] for k in ("before", "runs_review", "answer_missing")}, f,
                          ensure_ascii=False, indent=1)
            with open(out_dir / "split.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["qid", "doc_hash", "qtype", "old_q", "new_q",
                                                  "a_len", "conflict"], extrasaction="ignore")
                w.writeheader()
                for s in p["split"]:
                    w.writerow(s)
            with open(out_dir / "retire.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["qid", "doc_hash", "seq", "qtype", "reason", "q"],
                                   extrasaction="ignore")
                w.writeheader()
                for r in p["retire"]:
                    w.writerow(r)
        print(json.dumps(head, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
