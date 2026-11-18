# -*- coding: utf-8 -*-
"""question_freq：统计"同一个题目"在面试题 / 笔试题里的出现频次。

只做统计，不写库、不改文件：
  ① 抽取文本（pdf 走 pymupdf、docx 走 zip+XML、doc 走 Word COM）
  ② 切题：优先按题号标记（1. / 1、/ （1） / Q1 / 第1题 / 【1】）切块，
     块内取到第一个问号为止；无题号时按"含疑问词"的行兜底
  ③ 归一化 + SimHash(64bit, char bigram) + 16bit 分带 LSH 找近重复，并查集合并
  ④ 按"出现的文档数"排序输出 CSV / Markdown

用法：
    python tools/question_freq.py --dirs "31-收件箱\\面试题" "31-收件箱\\笔试"
    python tools/question_freq.py --dirs ... --csv build\\question_freq.csv --md docs\\07-题目频次.md
"""
from __future__ import annotations

import argparse
import atexit
import csv
import hashlib
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config  # noqa: E402

# 题号标记：行首的序号 / 括号序号 / Q1 / 第N题 / 【N】
Q_MARK = re.compile(
    r"^\s*(?:"
    r"\d{1,3}\s*[、.．)）]"          # 1. / 1、 / 1)
    r"|\(\s*\d{1,3}\s*\)"            # (1)
    r"|（\s*\d{1,3}\s*）"            # （1）
    r"|Q\s*\d{1,3}"                  # Q1
    r"|问题\s*\d{1,3}"               # 问题1
    r"|第\s*\d{1,3}\s*[题道]"        # 第1题
    r"|【\s*\d{1,3}\s*】"            # 【1】
    r")\s*")
# 无题号时的疑问兜底（笔试题常是祈使句，也收）
Q_WORD = re.compile(r"(什么|如何|为什么|怎么|区别|原理|简述|请说明|请写出|请描述|是否|哪些|多少|谈谈|比较)")
# 题号后面紧跟答案时的截断词
ANSWER_CUT = re.compile(r"(参考答案|答案\s*[:：]|答\s*[:：]|解析\s*[:：]|说明\s*[:：])")
PAGE_NOISE = re.compile(r"^(?:第?\s*\d+\s*页?|-\s*\d+\s*-|\d{1,4})$")
CLEAN = re.compile(r"[^\u4e00-\u9fa5a-z0-9]+")


# ---------------- 文本抽取 ----------------
DOC_EXTS = {".pdf", ".docx", ".doc"}


def text_of_pdf(path: Path) -> str:
    import pymupdf

    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def text_of_docx(path: Path) -> str:
    """docx 是 zip：直接读 word/document.xml，段落按 </w:p> 断行。不依赖 python-docx。"""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(ns + "p"):
        paras.append("".join(t.text or "" for t in p.iter(ns + "t")))
    return "\n".join(paras)


_WORD = None


def _word_app():
    """Word COM 会话（懒建、复用）。用 DispatchEx 起独立实例，不接管用户已打开的 Word。"""
    global _WORD
    if _WORD is None:
        import win32com.client as win32
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        _WORD = app
    return _WORD


def close_word() -> None:
    """显式退出 Word COM（幂等）。批量流程结束必须调用——否则 WINWORD.EXE 残留
    （2026-11-19 审核修复：此前全局单例从不 Quit，extract/cluster 各跑一次留一个宿主进程）。"""
    global _WORD
    app, _WORD = _WORD, None
    if app is None:
        return
    try:
        app.Quit()
    except Exception:  # noqa: BLE001  已崩溃的 Word 不值得再抛一次
        pass


atexit.register(close_word)     # 兜底：正常退出没显式关也不残留


@contextmanager
def word_app():
    """with 用法：块内可复用同一实例，退出即 Quit。供 extract / cluster 外层使用。"""
    try:
        yield _word_app()
    finally:
        close_word()


def text_of_doc(path: Path) -> str:
    """老 .doc 只能靠 Word（本机 Office 12.0）。失败返回空串，由上层记 skipped。"""
    try:
        __import__("win32com.client")      # 仅探依赖；实例统一由 _word_app 管理
    except ImportError:
        return ""
    d = _word_app().Documents.Open(str(path), ReadOnly=True)
    try:
        return d.Content.Text
    finally:
        d.Close(False)


def extract(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return text_of_pdf(path)
    if ext == ".docx":
        return text_of_docx(path)
    if ext == ".doc":
        return text_of_doc(path)
    return ""


# ---------------- 切题 ----------------
def split_qa(text: str) -> list[tuple[str, str]]:
    """把文本切成 (题干块, 答案块) 列表（均未归一化）。

    题干块仍需再过 `_clean_question` 才知是否真是题；答案块用于"同题不同答"的对抗判定
    （聚类删重只看题干会把"题目一样、答案不同"的两份资料判成重复）。
    """
    blocks: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            blocks.append(" ".join(buf))
            buf.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or PAGE_NOISE.match(line):
            continue
        if Q_MARK.match(line):
            flush()
            buf.append(Q_MARK.sub("", line, count=1))
            continue
        if buf:
            buf.append(line)
        elif Q_WORD.search(line):
            buf.append(line)

    flush()
    out = []
    for b in blocks:
        b = b.strip()
        m = ANSWER_CUT.search(b)        # 排版是"题号 问题 / 答案：…"连着走
        if m:
            out.append((b[: m.start()], b[m.end():].strip()))
        else:
            out.append((b, ""))
    return out


def _clean_question(b: str) -> str | None:
    """题干块 → 像问题的短句；不像题返回 None。"""
    b = b.strip().strip(" ：:；;，,")
    # ① 只留到第一个问号；没有问号就留到第一个句号（避免把整段代码/答案算成题）
    m = re.search(r"[？?]", b)
    if m:
        b = b[: m.start()]
    else:
        m = re.search(r"[。；;，,]", b)
        if m:
            b = b[: m.start()]
    b = b.strip(" ：:；;，,")
    # ② 必须是"像问题"的：带疑问/祈使词，或长度够但要短（长块基本是答案正文）
    if len(b) < 6 or len(b) > 120:
        return None
    if not (Q_WORD.search(b) or re.search(r"[？?]", b)):
        return None
    return b


def clean_question(b: str) -> str | None:
    """题干块 → 像问题的短句；不像题返回 None（split_questions 的对外口径）。"""
    return _clean_question(b)


def split_questions(text: str) -> list[str]:
    """返回题目块列表（未归一化）。"""
    out = []
    for q, _a in split_qa(text):
        cleaned = _clean_question(q)
        if cleaned:
            out.append(cleaned)
    return out


def normalize(q: str) -> str:
    return CLEAN.sub("", q.lower())


# ---------------- 近重复聚类 ----------------
def simhash(text: str) -> int:
    bits = [0] * 64
    if len(text) < 2:
        grams = [text]
    else:
        grams = [text[i:i + 2] for i in range(len(text) - 1)]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        for i in range(64):
            bits[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if bits[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def grams(text: str) -> set[str]:
    """字符 2-gram 集合：中文不切词时，Jaccard 比 SimHash 更能判"同一题的不同措辞"。"""
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def cluster(items: list[tuple[str, int]], max_dist: int, min_jac: float = 0.75) -> DSU:
    """items = [(归一化文本, simhash)]；16 bit 分带 LSH 取候选，再算汉明 + Jaccard 双重确认。

    只用汉明不管用：短问句（十来字）的 SimHash 抖动大，同义改写常差 20+ 位，
    而完全不同的题也可能撞进 12 位内。加一道 2-gram Jaccard 才稳。
    """
    dsu = DSU(len(items))
    gsets = [grams(t) for t, _ in items]
    buckets = defaultdict(list)
    for idx, (_, h) in enumerate(items):
        for band in range(4):
            buckets[(band, (h >> (band * 16)) & 0xFFFF)].append(idx)
    for cands in buckets.values():
        if len(cands) < 2:
            continue
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                a, b = cands[i], cands[j]
                if dsu.find(a) == dsu.find(b):
                    continue
                if hamming(items[a][1], items[b][1]) > max_dist:
                    continue
                if jaccard(gsets[a], gsets[b]) < min_jac:
                    continue
                dsu.union(a, b)
    return dsu


# ---------------- 主流程 ----------------
def iter_docs(dirs: list[Path]):
    for root in dirs:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in DOC_EXTS:
                    yield p


def kind_of(path: Path) -> str:
    """面试题 / 笔试 —— 按路径里最深一级含"笔试|面试"的目录判。"""
    for seg in reversed(path.parts):
        if "笔试" in seg:
            return "笔试"
        if "面试" in seg:
            return "面试"
    return "其他"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同一题目出现频次统计")
    ap.add_argument("--dirs", nargs="+", required=True, help="要统计的根目录（可多个）")
    ap.add_argument("--csv", default="", help="输出 CSV 路径")
    ap.add_argument("--md", default="", help="输出 Markdown 报告路径")
    ap.add_argument("--top", type=int, default=200, help="CSV 输出前 N 个簇")
    ap.add_argument("--dist", type=int, default=24, help="SimHash 候选阈值（短问句抖动大，只当候选用）")
    ap.add_argument("--jac", type=float, default=0.7, help="2-gram Jaccard 复核阈值")
    args = ap.parse_args(argv)

    roots = [Path(d) if Path(d).is_absolute() else config.CORPUS_ROOT / d for d in args.dirs]
    for r in roots:
        if not r.exists():
            print(f"[fatal] 目录不存在：{r}", file=sys.stderr)
            return 2

    occ: list[tuple[str, int, str, set, int]] = []   # (原文代表, simhash, 归一化, 文档集合, 次数)
    stats = Counter()
    for p in iter_docs(roots):
        try:
            text = extract(p)
        except Exception as ex:                     # noqa: BLE001 —— 单件失败不影响整体
            stats["error"] += 1
            print(f"[warn] 抽取失败 {p.name}: {type(ex).__name__}", file=sys.stderr)
            continue
        if len(text.strip()) < 80:
            stats["no_text"] += 1
            continue
        qs = split_questions(text)
        stats["docs"] += 1
        stats["questions"] += len(qs)
        rel = str(p.relative_to(config.CORPUS_ROOT)) if config.CORPUS_ROOT in p.parents else str(p)
        for q in qs:
            nq = normalize(q)
            if len(nq) < 6:
                stats["too_short"] += 1
                continue
            occ.append((q, simhash(nq), nq, {rel}, 1))

    if not occ:
        print("[info] 没有可统计的题目", file=sys.stderr)
        return 1

    # 先按归一化文本精确合并（同题不同排版），再做 SimHash 近重复合并
    exact = defaultdict(list)
    for i, item in enumerate(occ):
        exact[item[2]].append(i)

    items = [(it[2], it[1]) for it in occ]
    dsu = cluster(items, args.dist, args.jac)
    # 精确同文本强合并
    for _, idxs in exact.items():
        for k in idxs[1:]:
            dsu.union(idxs[0], k)

    groups = defaultdict(list)
    for i in range(len(occ)):
        groups[dsu.find(i)].append(i)

    agg = []
    for gid, idxs in groups.items():
        docs: set[str] = set()
        kind_docs = defaultdict(set)
        total = 0
        best = occ[idxs[0]][0]
        for i in idxs:
            docs |= occ[i][3]
            total += occ[i][4]
            if len(occ[i][0]) > len(best):
                best = occ[i][0]
        for d in docs:
            kind_docs[kind_of(Path(d))].add(d)
        agg.append({
            "question": best[:200],
            "doc_freq": len(docs),
            "occurrences": total,
            "面试文档": len(kind_docs["面试"]),
            "笔试文档": len(kind_docs["笔试"]),
            "variants": len(idxs),
            "sample": sorted(docs)[0],
        })

    agg.sort(key=lambda x: (-x["doc_freq"], -x["occurrences"], -len(x["question"])))
    top = agg[: args.top]

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["rank", "doc_freq", "occurrences", "面试文档", "笔试文档", "variants", "question", "sample"])
            for rank, row in enumerate(top, 1):
                w.writerow([rank, row["doc_freq"], row["occurrences"], row["面试文档"], row["笔试文档"],
                            row["variants"], row["question"], row["sample"]])
        print(f"[csv] {len(top)} 行 → {out}")

    print(f"[stat] 文档 {stats['docs']}，抽不出文本 {stats['no_text']}，异常 {stats['error']}，"
          f"题目块 {stats['questions']}，簇 {len(agg)}，≥2 文档的簇 "
          f"{sum(1 for r in agg if r['doc_freq'] >= 2)}")

    if args.md:
        md = Path(args.md)
        md.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 面试题 / 笔试题 同题频次报告",
            "",
            f"- 统计范围：{', '.join(str(r) for r in roots)}",
            f"- 文档 {stats['docs']}（抽不出文本 {stats['no_text']}，异常 {stats['error']}），"
            f"切出题目块 {stats['questions']}",
            f"- 聚成 {len(agg)} 个题簇，其中 ≥2 份文档重复的 "
            f"{sum(1 for r in agg if r['doc_freq'] >= 2)} 个",
            "",
            "| # | 出现文档 | 出现次数 | 面试 | 笔试 | 题目（最长变体，截断） |",
            "|---|---|---|---|---|---|",
        ]
        for rank, row in enumerate(top[:60], 1):
            q = row["question"].replace("|", "/")[:60]
            lines.append(f"| {rank} | {row['doc_freq']} | {row['occurrences']} | "
                         f"{row['面试文档']} | {row['笔试文档']} | {q} |")
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[md] {md}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
