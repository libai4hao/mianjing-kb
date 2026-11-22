# -*- coding: utf-8 -*-
"""question_freq：统计"同一个题目"在面试题 / 笔试题里的出现频次。

只做统计，不写库、不改文件：
  ① 抽取文本（pdf 走 pymupdf、docx 走 zip+XML、doc 走 Word COM）
  ② 切题：优先按题号标记（1. / 1、/ （1） / Q1 / 第1题 / 【1】）切块，
     块内取到第一个问号为止；无题号时按"含疑问词"的行兜底
  ③ 归一化 + 2-gram 倒排（前缀过滤）找近重复，并查集合并；SimHash 仅保留作辅助特征
  ④ 按"出现的文档数"排序输出 CSV / Markdown

用法：
    python -m mianjing_kb.question_freq --dirs "31-收件箱\\面试题" "31-收件箱\\笔试"
    python -m mianjing_kb.question_freq --dirs ... --csv build\\question_freq.csv --md docs\\07-题目频次.md
"""
from __future__ import annotations

import argparse
import atexit
import csv
import hashlib
import math
import os
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config  # noqa: E402
from mianjing_kb.kb import docx_safety

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
CLEAN = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffa-z0-9]+")
# 归一逻辑版本：NFKC、清洗规则或符号映射变化时递增；label_facts 用它标记存量标签的
# 可迁移口径，并可用 q_norm_sample 在升级后重算 hash 而不是整批废弃。
NORMALIZER_VERSION = config.NORMALIZER_VERSION
MIN_NORM = 4

# 代码符号先做语义映射，避免 `C++`/`C#`、`&&`/`&`、`i++`/`++i` 被 CLEAN 抹平后碰撞。
_CXX = re.compile(r"\bC\+\+", re.I)
_CSHARP = re.compile(r"\bC#", re.I)
_POST_INC = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\+\+")
_PRE_INC = re.compile(r"\+\+([A-Za-z_][A-Za-z0-9_]*)")
_POST_DEC = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\-\-")
_PRE_DEC = re.compile(r"\-\-([A-Za-z_][A-Za-z0-9_]*)")


def _map_symbols(text: str) -> str:
    """把有语义的代码符号映射成词，再做清洗；顺序保证 C++ 不被 ++ 规则拆坏。"""
    text = _CXX.sub("cpp", text)
    text = _CSHARP.sub("csharp", text)
    text = text.replace("&&", "and").replace("||", "or")
    text = text.replace("&", "bitand").replace("|", "bitor")
    text = _POST_INC.sub(r"\1postinc", text)
    text = _PRE_INC.sub(r"preinc\1", text)
    text = _POST_DEC.sub(r"\1postdec", text)
    text = _PRE_DEC.sub(r"predec\1", text)
    return text


# ---------------- 文本抽取 ----------------
DOC_EXTS = {".pdf", ".docx", ".doc"}


def text_of_pdf(path: Path) -> str:
    import pymupdf

    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text("text", sort=True) for page in doc)


def _docx_numbering(zf: zipfile.ZipFile) -> dict[str, dict[int, tuple[str, str, int]]]:
    """解析受大小限制的 word/numbering.xml：numId -> ilvl -> (numFmt, lvlText, start)。"""
    try:
        xml = docx_safety.read_docx_member(zf.filename, "word/numbering.xml",
                                           max_bytes=4 * 1024 * 1024)
        root = ET.fromstring(xml)
    except (KeyError, ET.ParseError, docx_safety.UnsafeDocx):
        return {}
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    abstract: dict[str, dict[int, tuple[str, str, int]]] = {}
    for a in root.iter(ns + "abstractNum"):
        aid = a.get(ns + "abstractNumId") or ""
        levels: dict[int, tuple[str, str, int]] = {}
        for lvl in a.iter(ns + "lvl"):
            ilvl = int(lvl.get(ns + "ilvl") or 0)
            fmt = lvl.find(ns + "numFmt")
            text = lvl.find(ns + "lvlText")
            start = lvl.find(ns + "start")
            levels[ilvl] = (
                (fmt.get(ns + "val") if fmt is not None else "decimal") or "decimal",
                (text.get(ns + "val") if text is not None else "%1.") or "%1.",
                int(start.get(ns + "val") if start is not None else 1),
            )
        abstract[aid] = levels
    nums: dict[str, dict[int, tuple[str, str, int]]] = {}
    for n in root.iter(ns + "num"):
        nid = n.get(ns + "numId") or ""
        aid_node = n.find(ns + "abstractNumId")
        aid = (aid_node.get(ns + "val") or "").strip() if aid_node is not None else ""
        nums[nid] = abstract.get(aid, {})
    return nums


def _roman(n: int) -> str:
    vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _number_label(fmt: str, n: int) -> str:
    if fmt == "decimal":
        return str(n)
    if fmt == "lowerLetter":
        return chr(ord("a") + (n - 1) % 26)
    if fmt == "upperLetter":
        return chr(ord("A") + (n - 1) % 26)
    if fmt in ("lowerRoman", "upperRoman"):
        s = _roman(n)
        return s.upper() if fmt == "upperRoman" else s
    if fmt == "bullet":
        return "•"
    return str(n)


def _docx_para_text(p, numbering: dict, counters: dict[str, dict[int, int]]) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    ppr = p.find(ns + "pPr")
    prefix = ""
    if ppr is not None:
        numpr = ppr.find(ns + "numPr")
        if numpr is not None:
            nid_node = numpr.find(ns + "numId")
            ilvl_node = numpr.find(ns + "ilvl")
            nid = (nid_node.get(ns + "val") if nid_node is not None else "") or ""
            ilvl = int((ilvl_node.get(ns + "val") if ilvl_node is not None else 0) or 0)
            levels = numbering.get(nid, {})
            level = levels.get(ilvl)
            if level:
                fmt, lvl_text, start = level
                cur = counters.setdefault(nid, {})
                cur[ilvl] = cur.get(ilvl, start - 1) + 1
                for deeper in [k for k in cur if k > ilvl]:
                    del cur[deeper]
                label = _number_label(fmt, cur[ilvl])
                prefix = lvl_text.replace(f"%{ilvl + 1}", label) + " "
    parts = [prefix]
    for node in p.iter():
        if node.tag == ns + "t":
            parts.append(node.text or "")
        elif node.tag == ns + "br":
            parts.append("\n")
        elif node.tag == ns + "tab":
            parts.append("\t")
    return "".join(parts)


def text_of_docx(path: Path) -> str:
    """docx 是 zip：直接读 word/document.xml；保留 w:br/w:tab 与自动编号前缀。"""
    with zipfile.ZipFile(path) as zf:
        xml = docx_safety.read_docx_member(path, "word/document.xml")
        numbering = _docx_numbering(zf)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    counters: dict[str, dict[int, int]] = {}
    paras = [_docx_para_text(p, numbering, counters) for p in root.iter(ns + "p")]
    return "\n".join(paras)


_WORD = None


def _word_app():
    """Word COM 会话（懒建、复用）。用 DispatchEx 起独立实例，不接管用户已打开的 Word。"""
    global _WORD
    if _WORD is None:
        import win32com.client as win32
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        # 禁宏、关提示：question_freq 可能打开不可信 .doc，绝不能让宏在自动化会话里执行。
        app.AutomationSecurity = 3       # msoAutomationSecurityForceDisable
        app.DisplayAlerts = 0            # wdAlertsNone
        _WORD = app
    return _WORD


def _open_word_doc(app, path: Path):
    """只读打开 .doc；不转换确认、不加入最近文件、不提示密码。"""
    return app.Documents.Open(
        str(path),
        ConfirmConversions=False,
        ReadOnly=True,
        AddToRecentFiles=False,
        PasswordDocument="",
    )


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
    """老 .doc 只能靠 Word（本机 Office 12.0）。失败抛异常，不能冒充空文档。

    打开失败重试一次：Word 崩溃后旧代理会拖死后续所有文档，必须 Quit 重建。
    """
    try:
        __import__("win32com.client")      # 仅探依赖；实例统一由 _word_app 管理
    except ImportError as exc:
        raise RuntimeError("win32com 不可用，无法读取 .doc") from exc
    app = None
    doc = None
    for attempt in range(2):
        try:
            app = _word_app()
            doc = _open_word_doc(app, path)
            break
        except Exception:
            close_word()
            if attempt == 1:
                raise
    try:
        return doc.Content.Text
    finally:
        try:
            doc.Close(False)
        except Exception:  # noqa: BLE001  已崩溃的 Word 不值得覆盖真实读取错误
            close_word()


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

    DEPRECATED：extract 已改用 chunker.chunk_v2；本函数只保留给 cluster 的旧
    缓存/对照路径，完成迁移后应删除。新代码不要再用它切题。
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
    text = unicodedata.normalize("NFKC", q)
    text = _map_symbols(text)
    return CLEAN.sub("", text.lower())


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


CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")
SHORT_CJK_MAX = 6          # 短题：归一后中文 ≤6 字，2-gram 分母太小，Jaccard 会被单个词抬高
SHORT_LEN_MAX = 20         # 短题：归一后整串 ≤20 字符；再长就不是"一个实体撑起相似度"的形态了


def latin_terms(text: str) -> set[str]:
    return {t.lower() for t in LATIN_TOKEN.findall(text or "")}


def short_latin_conflict(a: str, b: str) -> bool:
    """F6：短题里的拉丁实体不同 → 不是同一题，不论 Jaccard 多高。

    `什么是springmvc` 与 `什么是spring?` 的 2-gram Jaccard 有 0.727，越过 0.7 契约
    就被并进同一个 canonical；短题字符少，一个实体词就撑起相似度。这里用拉丁词项
    集合做闸门：两者都含拉丁词且集合不相等（差集非空）即判冲突。
    长题不走这条（中文 >6 字的句子里实体差异通常已被足够多的 2-gram 稀释）；
    整串 >20 字符的也不走——像 "How does HashMap resize work" 这类英文句子里，
    每个词都是拉丁词，"集合不等"并不代表实体不同，按长度放过避免误拆。
    """
    a, b = a or "", b or ""
    if min(len(a), len(b)) > SHORT_LEN_MAX:
        return False
    ca, cb = len(CJK_CHAR.findall(a)), len(CJK_CHAR.findall(b))
    if min(ca, cb) > SHORT_CJK_MAX:
        return False
    ta, tb = latin_terms(a), latin_terms(b)
    if not ta or not tb or ta == tb:
        return False
    return bool(ta ^ tb)


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


def _prefix_len(size: int, min_jac: float) -> int:
    """Jaccard 前缀过滤长度：len - ceil(t*len) + 1。

    全局按 2-gram 词频从稀到常排序后，Jaccard >= t 的两个集合必有一对相同 gram
    同时落在各自前缀里（prefix filtering 的标准结论）。
    """
    return max(1, size - math.ceil(min_jac * size) + 1)


def cluster(items: list[tuple[str, int]], max_dist: int = 24, min_jac: float = 0.75) -> DSU:
    """按 2-gram 倒排索引 + 前缀过滤聚类；Jaccard 阈值是精确的，不再依赖 SimHash 分带。

    `max_dist` 为兼容旧调用保留但不再参与判定：SimHash 只是近似召回，20bit 差异的
    同义改写会被 16bit LSH 漏掉，而 `--dist` 也不是有效的 Jaccard 阈值。这里先对
    归一化后的唯一文本建倒排，再用前缀过滤生成候选，最后按完整 Jaccard 精确复核。
    """
    unique: dict[str, list[int]] = {}
    for idx, (text, _h) in enumerate(items):
        unique.setdefault(text, []).append(idx)

    texts = list(unique)
    gsets = [grams(t) for t in texts]
    freq: dict[str, int] = {}
    for s in gsets:
        for g in s:
            freq[g] = freq.get(g, 0) + 1

    # 全局统一排序，保证两个集合的前缀使用同一顺序；稀 token 优先，降低候选量。
    order = sorted(freq, key=lambda g: (freq[g], g))
    rank = {g: i for i, g in enumerate(order)}
    postings: dict[str, list[int]] = defaultdict(list)
    for idx, s in enumerate(gsets):
        if not s:
            continue
        k = _prefix_len(len(s), min_jac)
        for g in sorted(s, key=lambda x: rank[x])[:k]:
            postings[g].append(idx)

    udsu = DSU(len(texts))
    for cands in postings.values():
        if len(cands) < 2:
            continue
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                a, b = cands[i], cands[j]
                if udsu.find(a) == udsu.find(b):
                    continue
                if jaccard(gsets[a], gsets[b]) < min_jac:
                    continue
                if short_latin_conflict(texts[a], texts[b]):   # F6：短题实体不同不并
                    continue
                udsu.union(a, b)

    # 唯一文本的簇扩展到原实例：同一文本的多次出现必然同簇，不参与 N×M 比对。
    full = DSU(len(items))
    groups: dict[int, list[int]] = defaultdict(list)
    for u_idx, idxs in enumerate(unique.values()):
        groups[udsu.find(u_idx)].extend(idxs)
    for idxs in groups.values():
        rep = idxs[0]
        for idx in idxs[1:]:
            full.union(rep, idx)
    return full


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
    ap.add_argument("--dist", type=int, default=24, help="已废弃：聚类改为 2-gram 倒排，仅保留参数兼容")
    ap.add_argument("--jac", type=float, default=0.7, help="2-gram Jaccard 阈值（精确判定）")
    args = ap.parse_args(argv)

    roots = [Path(d) if Path(d).is_absolute() else config.CORPUS_ROOT / d for d in args.dirs]
    for r in roots:
        if not r.exists():
            print(f"[fatal] 目录不存在：{r}", file=sys.stderr)
            return 2

    occ: list[tuple[str, int, str, set, int]] = []   # (原文代表, simhash, 归一化, 文档集合, 次数)
    stats = Counter()
    col_by_rel: dict[str, str] = {}
    rules = None
    try:
        from mianjing_kb.classify import Rules
        rules = Rules()
    except Exception:  # noqa: BLE001 —— 独立统计不应因分类配置缺失而失败
        rules = None
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
        try:
            col_by_rel[rel] = rules.collection(rel.replace("\\", "/")) if rules else ""
        except Exception:  # noqa: BLE001
            col_by_rel[rel] = ""
        for q in qs:
            nq = normalize(q)
            if len(nq) < MIN_NORM:
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
        collections = {col_by_rel.get(d, "") or f"doc:{d}" for d in docs}
        agg.append({
            "question": best[:200],
            "doc_freq": len(docs),
            "n_collections": len(collections),
            "occurrences": total,
            "面试文档": len(kind_docs["面试"]),
            "笔试文档": len(kind_docs["笔试"]),
            "variants": len(idxs),
            "sample": sorted(docs)[0],
        })

    agg.sort(key=lambda x: (-x["n_collections"], -x["doc_freq"], -x["occurrences"],
                            -len(x["question"])))
    top = agg[: args.top]

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["rank", "n_collections", "doc_freq", "occurrences", "面试文档", "笔试文档",
                        "variants", "question", "sample"])
            for rank, row in enumerate(top, 1):
                w.writerow([rank, row["n_collections"], row["doc_freq"], row["occurrences"],
                            row["面试文档"], row["笔试文档"],
                            row["variants"], row["question"], row["sample"]])
        print(f"[csv] {len(top)} 行 → {out}")

    print(f"[stat] 文档 {stats['docs']}，抽不出文本 {stats['no_text']}，异常 {stats['error']}，"
          f"题目块 {stats['questions']}，簇 {len(agg)}，≥2 来源集的簇 "
          f"{sum(1 for r in agg if r['n_collections'] >= 2)}")

    if args.md:
        md = Path(args.md)
        md.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 面试题 / 笔试题 同题频次报告",
            "",
            f"- 统计范围：{', '.join(str(r) for r in roots)}",
            f"- 文档 {stats['docs']}（抽不出文本 {stats['no_text']}，异常 {stats['error']}），"
            f"切出题目块 {stats['questions']}",
            f"- 聚成 {len(agg)} 个题簇，其中 ≥2 个来源集（collection）重复的 "
            f"{sum(1 for r in agg if r['n_collections'] >= 2)} 个",
            "",
            "| # | 来源集 | 出现文档 | 出现次数 | 面试 | 笔试 | 题目（最长变体，截断） |",
            "|---|---|---|---|---|---|---|",
        ]
        for rank, row in enumerate(top[:60], 1):
            q = row["question"].replace("|", "/")[:60]
            lines.append(f"| {rank} | {row['n_collections']} | {row['doc_freq']} | "
                         f"{row['occurrences']} | {row['面试文档']} | {row['笔试文档']} | {q} |")
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[md] {md}")

    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
