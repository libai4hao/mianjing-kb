# -*- coding: utf-8 -*-
"""切题器只读诊断 CLI（docs/61 §7 T42）。

四个子命令，全部只读：生产库以 `mode=ro` 打开，输出只落在 `--out`。

    python -m mianjing_kb.chunk_diag diag --kb <kb.sqlite> --corpus <md 根目录> [--out DIR] [--tag TAG]
    python -m mianjing_kb.chunk_diag recall --kb <kb> --corpus <root> [--baseline-rev REV|--baseline-file F]
    python -m mianjing_kb.chunk_diag regress-diff --kb <kb> --corpus <root> [--baseline-rev REV] [--docs FILE]
    python -m mianjing_kb.chunk_diag ablate --kb <kb> --corpus <root> [--docs FILE] [--rules R1,R2] [--out MD]
    python -m mianjing_kb.chunk_diag self-check

`diag` 输出 `summary-<tag>.json` / `docs-<tag>.json` / `items-<tag>.jsonl`：
items 是逐题明细（含 weak / a_bleed / new_only 等标记），summary 是口径汇总
（题数、弱题、空答案、ghost、bleed、叙事、对账 new_only/lost、coverage 分布）。

判据与 docs/61 一致：弱题 = `_heading_questionish` 为假；`q_tail` = 问号后还有
≥10 字；`q_multi` = ≥2 个问号；`lost` = 生产题键在本篇新切题里找不到。
"""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from mianjing_kb import chunker as C

RE_NUM = re.compile(r'^\s*(?:#{1,6}\s+)?(?:\d{1,3}\s*[.、．)）]|第\s*\d+\s*题|问题[一二三四五六七八九十\d]+)')
RE_NARR = re.compile(r'(面试官|然后就|然后面试|接着(问|聊|说|又)|问我|问了|跟我说|和我说|聊了|又开始|继续问|'
                     r'最后问|首先问|先问|上来就|开场|自我介绍完|问了一下|又问)')
RE_LABEL = re.compile(r'^(?:答案|解析|参考答案|答|解|扩展|补充|注意|示例|代码|总结|目录|前言|简介|概述|'
                      r'参考|思路|分析|解答|译文|题目|问题|原文|翻译)[：:]?$')
# 题干尾巴里的“答案标记”：只有这类尾巴是真答案被吞（T40），无标记的多为同题第二问或示例。
RE_TAIL_MARK = re.compile(r'(?:答案|参考答案|解析|思路|分析|解答|说明|答)\s*[：:]')
# 订阅源文本的目录根候选：`20-题库MD` 子目录在前，最后是根目录本身。
CORPUS_SUBDIRS = ("20-题库MD", "")


def norm_key(s):
    """题干对账键：去空白 + 小写（与 docs/61 同口径）。"""
    return re.sub(r"\s+", "", s or "").lower()


def resolve_md(corpus, rel):
    for sub in CORPUS_SUBDIRS:
        p = (corpus / sub / rel) if sub else (corpus / rel)
        if p.exists():
            return p
    return None


def open_ro(kb):
    """生产库只以只读方式打开（判据：诊断不得写库）。"""
    if not Path(kb).exists():
        raise SystemExit(f"[fatal] 库不存在：{kb}")
    return sqlite3.connect(f"file:{Path(kb).resolve().as_posix()}?mode=ro", uri=True)


def read_hash_list(path):
    """读 doc_hash 清单：一行一个，忽略空行与 `#` 注释。"""
    return [h.strip() for h in Path(path).read_text(encoding="utf-8").splitlines()
            if h.strip() and not h.strip().startswith("#")]


def load_production(conn):
    prod = defaultdict(list)
    for dh, q in conn.execute("select doc_hash, q_text from questions"):
        prod[dh].append(norm_key(q))
    docs = conn.execute(
        "select doc_hash, md_rel from document_products where md_rel is not null").fetchall()
    return prod, docs


def bleed_lines(a):
    """答案区里“非空行前置”的问句/编号问句行 = 疑似下一题题干被吞（E_ANSWER_BLEED）。"""
    hit, prev = [], ""
    for ln in (a or "").splitlines():
        s = ln.strip()
        if not s:
            prev = ""
            continue
        inner = C.strip_bold(s)
        if (prev and not prev.startswith(("|", ">", "#"))
                and not inner.endswith(("：", ":")) and not C._is_codeish(inner)
                and (C._is_strong_qline(inner)
                     or (RE_NUM.match(inner) and re.search(r"[?？]", inner) and len(inner) <= 120))):
            hit.append(inner[:60])
        prev = s
    return hit


def audit_items(items, prod_keys):
    """逐题判弱题/空答案/bleed/叙事/多问/题干带答案，并标 new_only。"""
    rows, f = [], Counter()
    for it in items:
        q, a = it["q"], it["a"] or ""
        ql = C._qnorm_len(q)
        rec = {"q": q, "a": a[:400], "conf": it["conf"], "qtype": it["qtype"],
               "new_only": norm_key(q) not in prod_keys}
        if not C._heading_questionish(q):
            f["weak"] += 1
            rec["weak"] = 1
        if ql <= 10:
            f["q_le10"] += 1
        if q.rstrip().endswith(("：", ":")):
            f["q_colon"] += 1
        if RE_NARR.search(q):
            f["q_narr"] += 1
        if len(re.findall(r"[?？]", q)) >= 2:
            f["q_multi"] += 1
            rec["q_multi"] = 1
        mq = re.search(r"[?？]", q)
        if mq and len(q) - mq.end() >= 10:
            f["q_tail_answer"] += 1
            rec["q_tail"] = 1
            if RE_TAIL_MARK.search(q[mq.end():]):
                f["q_tail_label"] += 1
            else:
                f["q_tail_free"] += 1
        if RE_LABEL.match(C.strip_bold(q.strip())):
            f["q_label"] += 1
        if not a.strip():
            f["a_empty"] += 1
            rec["a_empty"] = 1
        elif C._toc_ghost_answer(a):
            f["a_ghost"] += 1
        bl = bleed_lines(a)
        if bl:
            f["a_bleed"] += 1
            rec["a_bleed"] = bl[:3]
        rows.append(rec)
    return rows, f


def cmd_diag(args):
    conn = open_ro(args.kb)
    prod, docs = load_production(conn)
    conn.close()
    if args.docs:
        hashes = set(read_hash_list(args.docs))
        docs = [d for d in docs if d[0] in hashes]
    corpus = Path(args.corpus)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, all_items = [], []
    for dh, rel in docs:
        p = resolve_md(corpus, rel)
        if p is None:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        v2 = C.chunk_v2(text)
        pk = set(prod.get(dh, []))
        new_keys = {norm_key(it["q"]) for it in v2["items"]}
        its, f = audit_items(v2["items"], pk)
        for it in its:
            it["doc"] = rel
        all_items += its
        m = re.search(r"疑似漏题 (\d+) 处", v2["note"])
        rows.append({"doc": rel, "prod_n": len(pk), "items": len(v2["items"]),
                     "new_only": sum(1 for it in its if it["new_only"]),
                     "lost": sum(1 for k in pk if k not in new_keys),
                     "chars": len(text), "coverage": v2["coverage"],
                     "probe": len(v2["probe"]), "missed": int(m.group(1)) if m else 0,
                     **f})
    tot = Counter()
    for r in rows:
        for k in ("weak", "q_le10", "q_colon", "q_narr", "q_multi", "q_tail_answer",
                  "q_tail_label", "q_tail_free",
                  "q_label", "a_empty", "a_ghost", "a_bleed", "new_only", "lost", "missed"):
            tot[k] += r.get(k, 0)
    n = len(all_items)
    summary = {
        "tag": args.tag, "docs": len(rows), "items": n,
        "counts": dict(tot), "ratio": {k: round(v / n, 4) for k, v in tot.items()} if n else {},
        "conf": dict(Counter(i["conf"] for i in all_items)),
        "qtype": dict(Counter(i["qtype"] for i in all_items)),
        "cov_lt50": sum(1 for r in rows if r["coverage"] < 0.5),
        "cov_lt80": sum(1 for r in rows if r["coverage"] < 0.8),
        "zero_items": sum(1 for r in rows if r["items"] == 0),
        "docs_lost_gt0": sum(1 for r in rows if r["lost"] > 0),
        "top_weak": sorted(rows, key=lambda r: -r.get("weak", 0))[:15],
    }
    (out / f"summary-{args.tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
    (out / f"docs-{args.tag}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (out / f"items-{args.tag}.jsonl").write_text(
        "\n".join(json.dumps(i, ensure_ascii=False) for i in all_items), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("tag", "docs", "items", "cov_lt50", "cov_lt80",
                                              "zero_items")}, ensure_ascii=False))
    return 0


def load_baseline_chunker(args):
    """读基线版 chunker：`--baseline-file` 或 `--baseline-rev`（git show <rev>:<path>）。"""
    if args.baseline_file:
        path = Path(args.baseline_file)
    elif args.baseline_rev:
        import subprocess
        path = Path(tempfile.gettempdir()) / f"chunker_baseline_{args.baseline_rev[:8]}.py"
        src = subprocess.run(["git", "show", f"{args.baseline_rev}:mianjing_kb/chunker.py"],
                             capture_output=True, check=True).stdout
        path.write_bytes(src)
    else:
        return None
    spec = importlib.util.spec_from_file_location("chunker_baseline_diag", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iter_docs(args):
    """按 `--docs`（doc_hash 清单）或全库产出 (doc_hash, md_rel, 文本, 生产题键)。"""
    conn = open_ro(args.kb)
    prod, docs = load_production(conn)
    conn.close()
    if args.docs:
        hashes = set(read_hash_list(args.docs))
        docs = [d for d in docs if d[0] in hashes]
    corpus = Path(args.corpus)
    for dh, rel in docs:
        p = resolve_md(corpus, rel)
        if p is None:
            continue
        yield dh, rel, p.read_text(encoding="utf-8", errors="ignore"), prod.get(dh, [])


def out_file(out, default_name):
    """`--out` 以 `.md`/`.txt` 结尾视为文件路径，否则视为目录并拼默认文件名。"""
    p = Path(out)
    if p.suffix.lower() in (".md", ".txt", ".json"):
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    p.mkdir(parents=True, exist_ok=True)
    return p / default_name


def cmd_recall(args):
    base = load_baseline_chunker(args)
    if base is None:
        raise SystemExit("[fatal] recall 需要 --baseline-rev 或 --baseline-file")
    lost, found = [], []
    tot = Counter()
    for dh, rel, text, pk in iter_docs(args):
        pkeys = set(pk)
        omap = {norm_key(it["q"]): it["q"] for it in base.chunk_v2(text)["items"]}
        nmap = {norm_key(it["q"]): it["q"] for it in C.chunk_v2(text)["items"]}
        for k in pkeys:
            if k in omap and k not in nmap:
                lost.append((rel, omap[k]))
            elif k not in omap and k in nmap:
                found.append((rel, nmap[k]))
        tot["prod"] += len(pkeys)
        tot["lost_base"] += sum(1 for k in pkeys if k not in omap)
        tot["lost_cur"] += sum(1 for k in pkeys if k not in nmap)
    lines = [f"# 生产题召回差集（{len(lost) + len(found)} 条差异）", "",
             f"- 生产题键 {tot['prod']}；基线未命中 {tot['lost_base']}；当前未命中 {tot['lost_cur']}",
             f"- 新增丢失 {len(lost)}；新增找回 {len(found)}；净变化 {len(found) - len(lost)}", "",
             "## 新增丢失", ""]
    lines += [f"- [{rel[:40]}] {q[:100]}".replace("\n", " ") for rel, q in lost[:args.limit]]
    lines += ["", "## 新增找回", ""]
    lines += [f"- [{rel[:40]}] {q[:100]}".replace("\n", " ") for rel, q in found[:args.limit]]
    out_file(args.out, "recall-diff.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"lost_new": len(lost), "found_new": len(found),
                      "lost_base": tot["lost_base"], "lost_cur": tot["lost_cur"]}, ensure_ascii=False))
    return 0


def cmd_regress_diff(args):
    base = load_baseline_chunker(args)
    if base is None:
        raise SystemExit("[fatal] regress-diff 需要 --baseline-rev 或 --baseline-file")
    lost = gained = weak_new = 0
    lines = []
    for dh, rel, text, _pk in iter_docs(args):
        a = {norm_key(it["q"]): it for it in base.chunk_v2(text)["items"]}
        b = {norm_key(it["q"]): it for it in C.chunk_v2(text)["items"]}
        rem = [(dh, a[k]) for k in a if k not in b]
        add = [(dh, b[k]) for k in b if k not in a]
        wn = [it for _, it in add if not C._heading_questionish(it["q"])]
        lost += len(rem)
        gained += len(add)
        weak_new += len(wn)
        lines.append(f"== {dh[:8]} {rel}: {len(a)} -> {len(b)}  丢 {len(rem)} 增 {len(add)}  "
                     f"弱题新增 {len(wn)}")
        lines += [f"   -  {it['q'][:100]}".replace("\n", " ") for _, it in rem]
        lines += [f"   +  {it['q'][:100]}".replace("\n", " ") for _, it in add]
    lines.append("")
    lines.append(f"合计 丢 {lost} 增 {gained} 新增弱题 {weak_new}")
    out_file(args.out, "regress-diff.txt").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"lost": lost, "gained": gained, "weak_new": weak_new}, ensure_ascii=False))
    return 0


# 逐规则消融：把单条规则临时替换成“永不命中”，跑完立刻还原。
NEVER = re.compile(r"(?!x)x")
RULES = {
    "句中问号": lambda: setattr(C, "RE_INLINE_QMARK", NEVER),
    "题号小数": lambda: setattr(C, "RE_DEC_UNIT", NEVER),
    "框架问词": lambda: setattr(C, "_is_framed_question", lambda s: False),
    "陈述句标题闸": lambda: setattr(C, "_is_weak_evidence_sentence", lambda s: False),
    "编号陈述句": lambda: setattr(C, "_is_declarative_item", lambda q: False),
    "目录页码": lambda: setattr(C, "RE_TOC_PAGE_TAIL", NEVER),
    "结构标签": lambda: setattr(C, "_is_structural_label", lambda s: False),
    "标题探针": lambda: setattr(C, "_heading_probe", lambda lines, i, q: q),
    "编程题模式": lambda: setattr(C, "_is_coding_doc", lambda lines: False),
}
_SAVED = ("RE_INLINE_QMARK", "RE_DEC_UNIT", "RE_TOC_PAGE_TAIL", "_is_framed_question",
          "_is_weak_evidence_sentence", "_is_declarative_item", "_is_structural_label",
          "_heading_probe", "_is_coding_doc")


def _restore(orig):
    for k, v in orig.items():
        setattr(C, k, v)


def cmd_ablate(args):
    orig = {k: getattr(C, k) for k in _SAVED}
    docs = [(rel, text) for _dh, rel, text, _pk in iter_docs(args)]
    rules = args.rules.split(",") if args.rules else list(RULES)
    unknown = [r for r in rules if r not in RULES]
    if unknown:
        raise SystemExit(f"[fatal] 未知规则：{unknown}；可选 {list(RULES)}")

    def run():
        return [{norm_key(it["q"]): it for it in C.chunk_v2(t)["items"]} for _, t in docs]

    try:
        _restore(orig)
        full = run()
        lines = [f"# 切题规则消融（{len(docs)} 篇，全部开启 {sum(len(x) for x in full)} 题）", ""]
        for name in rules:
            _restore(orig)
            RULES[name]()
            part = run()
            _restore(orig)
            add = [(rel, it) for (rel, _), a, b in zip(docs, full, part)
                   for k, it in a.items() if k not in b]
            rem = [(rel, it) for (rel, _), a, b in zip(docs, full, part)
                   for k, it in b.items() if k not in a]
            lines += [f"## {name}：+{len(add)} / -{len(rem)}", ""]
            lines += [f"+ [{rel[:30]}] {it['q'][:90]}".replace("\n", " ") for rel, it in add[:args.limit]]
            lines += [f"- [{rel[:30]}] {it['q'][:90]}".replace("\n", " ") for rel, it in rem[:args.limit]]
            lines.append("")
    finally:
        _restore(orig)
    out = out_file(args.out, "ablation.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"rules": rules, "docs": len(docs), "written": str(out)}, ensure_ascii=False))
    return 0


SELF_CHECK_MD = [
    "# 自检文档一\n\n## 1. 什么是 JVM 的类加载？\n\n类加载分五步。\n\n"
    "## 2. 3. PriorityBlockingQueue：支持优先级排序的无界阻塞队列。\n\n"
    "## 3. 简述栈和队列的区别并解释含义１１０\n\n栈后进先出。\n",
    "# 自检文档二（编程题）\n\n### 一. 编程题\n\n1. 峰值元素是指其值大于左右相邻值的元素。\n\n"
    "输入描述：第一行是数组长度。\n",
]


def cmd_self_check(_args):
    """不依赖生产库与语料的冒烟：固定两篇 md + 一组合成 item 跑一遍判据。"""
    items = [it for md in SELF_CHECK_MD for it in C.chunk_v2(md)["items"]]
    assert items, "自检：没有切出任何题"
    # 编程题文档的陈述句题干不应被降级（T38）
    assert any(it["q"].startswith("峰值元素") for it in items), "自检：编程题模式失效"
    # 编号陈述句仍留在父题答案（P6）
    assert not any(it["q"].startswith("PriorityBlockingQueue") for it in items), "自检：P6 失效"
    # 目录页码尾巴应被去掉（T39）
    assert not any("１１０" in it["q"] for it in items), "自检：页码尾巴未去除"
    # 判据本身：题干带答案、空答案、多问各计一次
    probe = [{"q": "什么是 X？答案：略写一段超过十个字的解释。", "a": "", "conf": "high", "qtype": "八股"},
             {"q": "A 是什么？B 是什么？", "a": "答：两段说明，长度足够。", "conf": "high", "qtype": "八股"}]
    _rows, f = audit_items(probe, set())
    assert f["q_tail_answer"] == 1 and f["a_empty"] == 1 and f["q_multi"] == 1, f"自检：判据计数异常 {dict(f)}"
    print(json.dumps({"items": len(items), "counts": dict(f)}, ensure_ascii=False))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m mianjing_kb.chunk_diag",
        description="切题器只读诊断（docs/61）：全量指标、生产题对账、回归差集、逐规则消融。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, kb_required=True):
        p.add_argument("--kb", required=kb_required, help="生产库路径（只读打开）")
        p.add_argument("--corpus", required=kb_required, help="md 语料根目录（含 20-题库MD 或直接是根）")
        p.add_argument("--out", default="chunk_diag_out", help="输出目录，默认 chunk_diag_out")
        p.add_argument("--docs", default="", help="doc_hash 清单文件；默认全库")
        p.add_argument("--limit", type=int, default=40, help="明细/消融每条规则最多列几条")

    p = sub.add_parser("diag", help="全量诊断：summary/docs/items（只读）")
    common(p)
    p.add_argument("--tag", default="base", help="输出文件名后缀，默认 base")
    p.set_defaults(func=cmd_diag)

    p = sub.add_parser("recall", help="生产题召回差集：基线版 vs 当前")
    common(p)
    p.add_argument("--baseline-file", default="", help="基线 chunker.py 路径")
    p.add_argument("--baseline-rev", default="", help="基线 git 修订（git show <rev>:mianjing_kb/chunker.py）")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("regress-diff", help="回归集差集：基线版 vs 当前")
    common(p)
    p.add_argument("--baseline-file", default="")
    p.add_argument("--baseline-rev", default="")
    p.set_defaults(func=cmd_regress_diff)

    p = sub.add_parser("ablate", help="逐规则消融：临时关掉单条规则")
    common(p)
    p.add_argument("--rules", default="", help=f"逗号分隔，默认全部：{','.join(RULES)}")
    p.set_defaults(func=cmd_ablate, out="chunk_diag_out/ablation.md")

    p = sub.add_parser("self-check", help="离线冒烟：不依赖生产库与语料")
    p.set_defaults(func=cmd_self_check)

    return ap


def main(argv=None):
    if os.environ.get("MJKB_CHUNK_DIAG_STRICT_READONLY"):     # 预留：CI 里可校验只读
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
