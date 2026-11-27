# -*- coding: utf-8 -*-
"""gate：文本质量门禁（三态 pass / review / fail，先 NFKC 归一）。

P0-2 修复（2026-11-17 审核）：本脚本原为四态（pass/garbled/low_cjk/non_content）、
--out 写 {"summary","results"}——与 dispatch.py / ocr_merge.py 消费的 gate_report.json
（rows + state 三态）结构不兼容，生产报告此前实际由语料侧 21-面经数据库\\tools\\gate.py
生成。本次把三态判定与 rows 结构统一收进本仓库，语料侧同名脚本降级为历史参考。

两个入口：
  --md-dir <dir>   扫描任意 md 目录（独立门禁：测试 / 新目录），rows.rel = md 相对路径
  --audit          生产口径：按 BUILD/convert_log.json 的 ok 件驱动，
                   rows.rel = 源文件相对语料根路径 —— 与 dispatch / ocr_merge 直接兼容

判据（先 NFKC 归一，去掉空白/Markdown 语法/分页标记，再算指标）：
    可见字符数 < 100   -> fail    极短产物
    异常字符比 >= 2%    -> fail    真乱码（ړ ୗ ᭗ 这类非中文码位）
    CJK 比例 < 15%     -> review  低中文占比，人工/OCR 裁定（英文书与乱码都落这里，不硬判）
    其余               -> pass

为什么必须 NFKC：PDF 提取常把汉字映射进 Kangxi 部首区（U+2F00），"大厂" 会变成 "⼤厂"。
内容可读，但检索、去重、聚合全部失效。归一后健康件异常率 4% 降到 0.2%。

判定语义边界：`pass` 只表示“没有可识别的非中文垃圾字符 + CJK/长度达标”，
**不等于文本可读或语义正确**。ToUnicode 映射错误把乱码映射成正常汉字时，字符白名单
识别不出来；需要人工抽检、字符二元组可信度或重复字符占比等额外手段。

gate_results 台账（--md-dir / --audit 模式，--no-db 跳过）仍用 schema 001 的四态词表
（pass|low_cjk|garbled|non_content）。三态报告的 review 映射 low_cjk，fail 按原因
映射 garbled（乱码）或 non_content（过短/缺产物）。--audit 同时把同一结论回写到
source_documents.gate_result，供 extract 前置过滤（T30）。

用法：
    python -m mianjing_kb.gate --audit                          # 生产：报告 + gate_results 回写
    python -m mianjing_kb.gate --md-dir "%CORPUS_ROOT%\\20-题库MD" --top 20
    python -m mianjing_kb.gate --md-dir ... --no-db --out report.json
    python -m mianjing_kb.gate --sentinel                       # 哨兵回归集：判定不退化校验
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, hashing, paths, textio, textmetrics, wlock  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"

# 三态阈值（对齐语料侧 gate.py，2026-11-17 审核后统一到本仓库）
# F10（2026-11-19）口径说明：TH_CJK 与 config.GATE_CJK_MIN 同义，单源引用；
# TH_BAD/TH_MIN 与 config 的 GATE_GARBLED_MAX/GATE_MIN_VISIBLE 是**不同指标**
# （三态报告 vs 四态台账），有意不合并数值。
TH_BAD = 0.02     # 异常字符比上限（>= 判 fail）
TH_CJK = config.GATE_CJK_MIN   # CJK 比例下限（< 判 review），与 config 单源
TH_MIN = config.TEXT_MIN_CHARS   # 最少可见字符数（< 判 fail），与 extract 单源
SENTINEL_MISS_MAX = 0.50   # 哨兵 MISS 占比超过此值 → 直接失败，不打印“全绿”

CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经",
           "个人面经": "个人面经", "专题笔记": "专题笔记"}
ILLEGAL = r'[\\/:*?"<>|\r\n\t]'
SOURCE_EXTS = {".pdf", ".doc", ".docx", ".md", ".txt", ".pptx", ".xlsx", ".html"}
HASH_SUFFIX = re.compile(r"(?:__|_)([0-9a-f]{8})$")
PAGE_MARK = re.compile(r"^[ \t]*=====[ \t]*page[ \t]+\d+[ \t]*=====[ \t]*$", re.I | re.M)
CODE_FENCE = re.compile(r"```.*?```", re.S)


def read_text_info(path: Path) -> tuple[str, str]:
    """兼容入口：统一委托 kb/textio（utf-8-sig → gb18030 → replace）。"""
    return textio.read_text_info(path)


def read_text(path: Path) -> str:
    return textio.read_text(path)


def safe(s: str) -> str:
    return re.sub(ILLEGAL, "_", s or "").strip()[:80]


def _clean_for_metrics(raw: str) -> str:
    text = PAGE_MARK.sub("", raw)
    text = CODE_FENCE.sub("", text)
    return textmetrics.strip_markdown_noise(text)


def _is_bad_char(c: str) -> bool:
    """异常字符：明确不该出现在中文文档里的码位。

    不把 emoji / 希腊字母 / 假名 / CJK 扩展汉字判坏；U+25A0–25FF 方框单独判坏，
    这是 PDF 字体映射失败的典型特征。
    """
    o = ord(c)
    if c.isspace() or o in (0x200B, 0x200C, 0x200D, 0xFE0F, 0xFEFF):
        return False
    if o < 0x80:
        return False
    if textmetrics.is_cjk(c):
        return False
    if 0x25A0 <= o <= 0x25FF:          # ■ □ 等方框乱码
        return True
    if 0xE000 <= o <= 0xF8FF or o == 0xFFFD:
        return True
    if 0xFE00 <= o <= 0xFE0F:          # variation selector
        return False
    # 合法符号/字母区：标点、货币、数字形式、箭头、数学符号、带圈数字、假名、谚文
    if (0x00A0 <= o <= 0x024F or 0x0370 <= o <= 0x03FF or 0x0400 <= o <= 0x04FF
            or 0x2000 <= o <= 0x206F or 0x2070 <= o <= 0x209F
            or 0x20A0 <= o <= 0x20CF or 0x2100 <= o <= 0x214F
            or 0x2150 <= o <= 0x218F or 0x2190 <= o <= 0x21FF
            or 0x2200 <= o <= 0x22FF or 0x2460 <= o <= 0x24FF
            or 0x3000 <= o <= 0x303F or 0x3040 <= o <= 0x30FF
            or 0x31F0 <= o <= 0x31FF or 0xAC00 <= o <= 0xD7AF
            or 0xFF00 <= o <= 0xFFEF or 0x1F000 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF):
        return False
    if textmetrics.is_garbled(c):
        return True
    return True


def metrics(raw: str) -> dict:
    """NFKC 归一后算三态指标。

    chars 为**可见字符数**（去掉空白、Markdown 语法、代码围栏、分页标记）；
    cjk / latin / bad 均以可见字符数为分母；fixed 为 NFKC 实际改写字符数。
    """
    text = unicodedata.normalize("NFKC", _clean_for_metrics(raw))
    vis = textmetrics.visible_chars(text)
    fixed = sum(1 for a, b in zip(raw, unicodedata.normalize("NFKC", raw)) if a != b)
    fixed += abs(len(raw) - len(unicodedata.normalize("NFKC", raw)))
    if vis == 0:
        return dict(chars=0, cjk=0.0, latin=0.0, bad=1.0, fixed=fixed, nfkc_changed=fixed)
    clean = re.sub(r"[\s\u200b-\u200d\ufeff]+", "", text)
    n = len(clean)
    if n == 0:
        return dict(chars=0, cjk=0.0, latin=0.0, bad=1.0, fixed=fixed, nfkc_changed=fixed)
    cjk = sum(1 for c in clean if textmetrics.is_cjk(c))
    latin = sum(1 for c in clean if ord(c) < 0x80 and c.isalpha())
    bad = sum(1 for c in clean if _is_bad_char(c))
    return dict(chars=n, cjk=cjk / n, latin=latin / n, bad=bad / n,
                fixed=fixed, nfkc_changed=fixed)


def judge(m: dict) -> tuple[str, str]:
    """三态判定：fail（真乱码/过短）→ review（低 CJK 灰区）→ pass。"""
    if m["chars"] < TH_MIN:
        return "fail", f"产物过短({m['chars']}字符)"
    if m["bad"] >= TH_BAD:
        return "fail", "乱码(异常字符%.1f%%)" % (m["bad"] * 100)
    if m["cjk"] < TH_CJK:
        return "review", "低CJK(%.1f%%) 需裁定" % (m["cjk"] * 100)
    return "pass", ""


def head_of(raw: str, n: int = 160) -> str:
    return re.sub(r"\s+", " ", raw[: n * 2])[:n]


def md_path_for(e: dict) -> Path:
    """registry 行 → 约定产物路径：<QUESTION_MD_DIR>/<category>/<topic>/<stem>.md

    只有标题确实带源文件扩展名时才剥扩展名；`v3.0-JavaGuide...` 这种带点的 stem
    不能被 splitext 在最后一个点截断。
    """
    cat = CAT_MAP.get(e.get("category"), "综合")
    topic = safe(e.get("topic")) or "综合"
    raw = e.get("title") or Path(e["rel"]).name
    suffix = Path(raw).suffix.lower()
    stem = raw[:-len(suffix)] if suffix in SOURCE_EXTS else raw
    return Path(config.QUESTION_MD_DIR) / cat / topic / (safe(stem) + ".md")


def path_key(p: str | Path) -> str:
    return str(p).replace("\\", "/").casefold()


def md_index() -> dict[str, list[str]]:
    """20-题库MD 全量 basename 索引（casefold，和 collisions_of 同一套规则）。

    只收语料目录内的 md：junction/软链指到语料外的文件不能进索引，否则 gate 会把
    语料外的正文当产物读、把哈希写进报告。
    """
    idx: dict[str, list[str]] = {}
    root_r = Path(config.QUESTION_MD_DIR).resolve()
    for root, _dn, files in os.walk(config.QUESTION_MD_DIR):
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            p = Path(root) / fn
            try:
                if not p.resolve().is_relative_to(root_r):
                    continue
            except OSError:
                continue
            idx.setdefault(fn.casefold(), []).append(str(p))
    return idx


def collisions_of(rows: list[dict]) -> list[dict]:
    """同一 md 被多个源写入：用行内 doc_hash 区分 同内容重复 / 真覆盖(丢内容)。

    - exists=False 的缺失产物不参与（它们没有文件，不能算覆盖）；
    - 同一 rel 的多条 convert_log 记录先去重（重试不是冲突）；
    - 缺 doc_hash 时标“无法判定”，不冒充真冲突。
    """
    groups: dict[str, dict] = {}
    for r in rows:
        if not r.get("exists", False):
            continue
        key = path_key(r["md"])
        g = groups.setdefault(key, {"md": r["md"], "rels": {}})
        g["rels"].setdefault(r["rel"], r.get("doc_hash"))
    out = []
    for g in groups.values():
        rels = g["rels"]
        if len(rels) < 2:
            continue
        hashes = [h for h in rels.values() if h]
        if len(set(hashes)) > 1:
            kind = "真覆盖(丢内容)"
        elif len(hashes) == len(rels):
            kind = "同内容重复"
        else:
            kind = "无法判定(缺doc_hash)"
        out.append(dict(md=g["md"], rels=list(rels), kind=kind))
    return out


def _rel_key(rel: str) -> str:
    return (rel or "").replace("\\", "/")


def _doc_hash_for(rel: str, e: dict) -> str | None:
    dh = e.get("doc_hash")
    if dh:
        return dh
    # rel 来自 convert_log（可被手改）：必须落在语料根内，绝对路径/`..`/链接逃逸视为缺失
    try:
        src = paths.safe_join(config.CORPUS_ROOT, rel, must_exist=True)
    except paths.UnsafePath:
        return None
    return hashing.sha256_file(src)[:16]


def _source_identity() -> dict[str, str] | None:
    """doc_hash → 台账 rel（#28：registry 身份必须与 source_documents 对账）。

    台账库不存在/打不开时返回 None（跳过对账，保持独立扫描语义）。
    """
    try:
        conn = db.connect(config.DB_PATH, create=False, readonly=True)
    except (FileNotFoundError, sqlite3.Error, OSError):
        return None
    try:
        out = {}
        for r in conn.execute("SELECT doc_hash, dir_path, file_name FROM source_documents"):
            rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
            out[r["doc_hash"]] = _rel_key(rel)
        return out
    finally:
        conn.close()


def _reconcile_doc_hash(rel: str, doc_hash: str | None,
                        src_ident: dict[str, str] | None) -> tuple[str | None, bool]:
    """registry hash 与台账源行对账；指向别的源记录时弃用并按源件字节重算。

    只在能**证明挂错**（hash 在台账里、但对应的是别的 rel）时才纠正；
    台账里没有该 hash（未入库的新源/独立扫描）保持原行为，不误伤 hash8 变体绑定。
    """
    if not doc_hash or src_ident is None:
        return doc_hash, False
    if src_ident.get(doc_hash, _rel_key(rel)) == _rel_key(rel):
        return doc_hash, False
    fixed = _doc_hash_for(rel, {})
    if fixed and src_ident.get(fixed) != _rel_key(rel):
        fixed = None
    return fixed, True


def run_audit() -> dict:
    """生产口径：convert_log ok 件 → 逐件找产物 md → 三态判定 → rows + collisions。

    审计候选加固（2026-11-27，docs/63 §3 P1-A）：
    - #28 registry doc_hash 与台账源行对账：指向别的源记录时弃用，改按源件字节重算；
    - #27 每行记录 md_hash（sha256），extract --require-gate 按它拒绝陈旧 pass；
    - #26 真覆盖冲突的源一律降 fail（fail-closed），不再发布可抽题的 pass。
    """
    build = Path(config.BUILD_DIR)
    reg = json.loads((build / "registry.json").read_text(encoding="utf-8"))
    log = json.loads((build / "convert_log.json").read_text(encoding="utf-8"))
    by_rel = {_rel_key(e["rel"]): e for e in reg}
    ok, seen, dup_log = [], set(), 0
    for l in log:
        if not l.get("ok"):
            continue
        k = _rel_key(l["rel"])
        if k in seen:
            dup_log += 1
            continue
        seen.add(k)
        ok.append(l)

    src_ident = _source_identity()
    idx = md_index()
    rows, skipped_reg, identity_mismatch = [], 0, 0
    for l in ok:
        e = by_rel.get(_rel_key(l["rel"]))
        if not e:
            skipped_reg += 1
            continue
        doc_hash = _doc_hash_for(l["rel"], e)
        doc_hash, mismatch = _reconcile_doc_hash(l["rel"], doc_hash, src_ident)
        if mismatch:
            identity_mismatch += 1
        probe = dict(e)
        probe["doc_hash"] = doc_hash
        canonical_path = md_path_for(probe)
        p = resolve_md(probe, idx)
        drift = bool(p and path_key(p) != path_key(canonical_path)
                     and not _is_hash_variant_of(p, canonical_path))
        if p is None:
            p = canonical_path
            rows.append(dict(rel=l["rel"], md=str(p), doc_hash=doc_hash, exists=False,
                             md_hash=None, state="fail", reason="产物缺失", drift=False))
            continue
        raw, enc = read_text_info(p)
        m = metrics(raw)
        state, why = judge(m)
        try:
            md_hash = hashing.sha256_file(p)
        except OSError:
            md_hash = None
        rows.append(dict(rel=l["rel"], md=str(p), doc_hash=doc_hash, exists=True, drift=drift,
                         md_hash=md_hash, state=state, reason=why, chars=m["chars"],
                         cjk=round(m["cjk"], 4), latin=round(m["latin"], 4),
                         bad=round(m["bad"], 4), fixed=m["fixed"], nfkc_changed=m["nfkc_changed"],
                         encoding=enc, head=head_of(raw)))

    colls = collisions_of(rows)
    # #26 fail-closed：真覆盖（不同源写同一 md，内容已丢）涉及的所有源降 fail，
    # 无论正文指标多好都不发布 pass —— 被覆盖源的正文根本不在盘上。
    real_keys = {path_key(c["md"]) for c in colls if c["kind"].startswith("真")}
    for r in rows:
        if r.get("exists") and path_key(r["md"]) in real_keys:
            r["state"] = "fail"
            r["reason"] = "真覆盖冲突：同一md被多个不同源写入"
            r["collision"] = True
    sp = [r for r in rows if r["state"] == "pass"]
    sr = [r for r in rows if r["state"] == "review"]
    sf = [r for r in rows if r["state"] == "fail"]
    real = sum(1 for c in colls if c["kind"].startswith("真"))
    drift = sum(1 for r in rows if r.get("drift"))
    rate = (len(sf) + len(sr)) / max(len(rows), 1)
    return {
        "summary": dict(converted=len(ok), examined=len(rows),
                        passed=len(sp), review=len(sr), failed=len(sf),
                        abnormal_rate=round(rate, 4), collisions=len(colls),
                        real_collisions=real, drift=drift,
                        skipped_no_registry=skipped_reg, skipped_duplicate_log=dup_log,
                        identity_mismatch=identity_mismatch,
                        missing=sum(1 for r in rows if not r.get("exists"))),
        "thresholds": dict(bad=TH_BAD, cjk=TH_CJK, min_chars=TH_MIN, nfkc=True),
        "rows": rows,
        "collisions": colls,
    }


# ---------------- 哨兵回归集（语料侧 gate.py --sentinel 移植，2026-11-17） ----------------
# 用途：judge / 转换链 / OCR 任何改动后跑一遍，确认已知样本判定不退化。
# 期望值校准说明（docs/17 §5）：
#   1) v1.0/V1.1/v2.0 的 md 镜像已按 retire_log 退役 → 产物缺失按 fail 判（与 --audit rows 同口径）；
#   2) 数据结构/剑指Offer/合并版大厂/操作系统 4 件经 P2-⑦ OCR 定稿合并（docs/09）后乱码清零，
#      探针实测均带 ===== page n ===== 页标记 → 原 fail/review 期望更新为 pass。
CASES = [
    (r"12-综合题库\v1.0-JavaGuide面试突击版.pdf", "fail"),
    (r"12-综合题库\V1.1-JavaGuide面试突击版.pdf", "fail"),
    (r"12-综合题库\v2.0-JavaGuide面试突击版.pdf", "fail"),
    (r"09-算法与数据结构\数据结构面试题.pdf", "pass"),              # 原 fail：OCR 合并后转 pass
    (r"09-算法与数据结构\剑指Offer精选题（含答案）.pdf", "pass"),    # 原 review：OCR 合并后转 pass
    (r"09-算法与数据结构\【合并版】大厂算法面试合集.pdf", "pass"),   # 原 review：OCR 合并后转 pass
    (r"08-计算机网络与操作系统\操作系统面试题（带答案）.pdf", "pass"),  # 原 fail：OCR 合并后转 pass
    (r"11-面经与真题\11.3-专题总结包\Java学习路线\Java 后端学习路线概览.pdf", "fail"),
    (r"12-综合题库\原创PDF面试手册\JavaGuide面试突击版（必看）\JavaGuide面试突击版5.0.pdf", "pass"),
    (r"12-综合题库\原创PDF面试手册\JavaGuide面试突击版（必看）\JavaGuide面试突击版5.0(暗黑版).pdf", "pass"),
    (r"12-综合题库\原创PDF面试手册\计算机基础\操作系统\图解操作系统-亮白风格.pdf", "pass"),
    (r"12-综合题库\原创PDF面试手册\计算机基础\操作系统\图解操作系统-暗黑风格.pdf", "pass"),
    (r"12-综合题库\Markdown笔记\JavaGuide面试突击版\v3.0-JavaGuide面试突击版.md", "pass"),
]

PAIRS = [
    ("同件", r"12-综合题库\Java面试知识点整理手册.pdf",
             r"12-综合题库\整理的多家公司常见面试题库350道.pdf"),
    ("不同件", r"12-综合题库\整理的多家公司常见面试题库350道.pdf",
               r"12-综合题库\JAVA常见面试题库\整理的多家公司常见面试题库350道.pdf"),
]


def pix_fp(pdf_path: Path, dpi: int = 72, with_meta: bool = False):
    """PDF 像素指纹：采样 10%/35%/60%/85% 四页渲染 md5 拼接（语料侧同款）。

    with_meta=True 时返回 {"fp", "page_count", "dpi"}，便于区分 PyMuPDF 版本/页数差异。
    """
    import pymupdf
    with pymupdf.open(str(pdf_path)) as doc:
        n = doc.page_count
        picks = sorted({min(int(n * r), n - 1) for r in (0.1, 0.35, 0.6, 0.85)})
        fp = "".join(hashlib.md5(doc[i].get_pixmap(dpi=dpi).samples).hexdigest()[:8]
                     for i in picks)
    return {"fp": fp, "page_count": n, "dpi": dpi} if with_meta else fp


def _md_has_doc_hash(p: Path, expected: str) -> bool:
    if not expected:
        return False
    try:
        text = read_text(p)
    except OSError:
        return False
    return expected in text


def _is_hash_variant_of(resolved: Path, canonical: Path) -> bool:
    """F5：`stem__<hash8>.md` / `stem_<hash8>.md` 是同目录同 stem 的正常产物，不算漂移。

    `resolve_md` 只在 hash8 与本文档 doc_hash 前 8 位一致时才绑定哈希变体；
    所以走到这里的变体已经证明“指向同一份文档”。真正指向别的文档/别的目录才算 drift。
    """
    if path_key(resolved.parent) != path_key(canonical.parent):
        return False
    m = HASH_SUFFIX.search(resolved.stem)
    return bool(m) and resolved.stem[:m.start()] == canonical.stem


def resolve_md(e: dict, idx: dict[str, list[str]]) -> Path | None:
    """registry 行 → 产物 md：canonical → 同目录同名漂移 → 哈希变体 → None。

    - 同名漂移只有在同一个主题目录（parent 相同）或 md 元数据含 doc_hash 时才绑定；
      否则 README.md / 面经.md 这类通用名会串到无关文件。
    - 哈希变体只认 doc_hash 前 8 位完全匹配的那一个；不盲目取 variants[0]。
    """
    p = md_path_for(e)
    if p.exists():
        return p
    expected = (e.get("doc_hash") or "")[:8]
    cands = idx.get(p.name.casefold()) or []
    if len(cands) == 1:
        cp = Path(cands[0])
        if path_key(cp.parent) == path_key(p.parent) or _md_has_doc_hash(cp, expected):
            return cp
    if p.parent.exists():
        variants = {}
        for pat in (p.stem + "__*.md", p.stem + "_*.md"):
            for v in p.parent.glob(pat):
                variants[v] = None
        hits = [v for v in variants
                if (m := HASH_SUFFIX.search(v.stem)) and m.group(1) == expected]
        if len(hits) == 1:
            return hits[0]
    return None


def run_sentinel() -> bool:
    """跑哨兵：判定与期望不符（退化）→ False；registry 缺行 / 源件缺失记 MISS。

    防护：MISS 占全部检查项超过 SENTINEL_MISS_MAX，或一项都没真正检查 → 直接失败，
    不再把“全部 MISS”打印成“全绿”。
    """
    build = Path(config.BUILD_DIR)
    reg = json.loads((build / "registry.json").read_text(encoding="utf-8"))
    by_rel = {_rel_key(e["rel"]): e for e in reg}
    idx = md_index()
    passed = miss = 0
    print(f"-- 单件判定哨兵（{len(CASES)} 件）--")
    for rel, want in CASES:
        e = by_rel.get(_rel_key(rel))
        if not e:
            print(f"  MISS registry 无此行  {rel}")
            miss += 1
            continue
        p = resolve_md(e, idx)
        if p is None:
            got, why = "fail", "产物缺失"     # 与 --audit rows 同口径（md 已退役也落这里）
        else:
            m = metrics(read_text(p))
            got, why = judge(m)
        flag = got == want
        passed += flag
        print(f"  {'OK  ' if flag else 'FAIL'} want={want:<6} got={got:<6} {why[:14]:<14} {rel[-44:]}")
    print(f"\n-- 重复件哨兵（像素指纹，{len(PAIRS)} 对）--")
    pair_checked = pair_fail = pair_miss = 0
    for want, a, b in PAIRS:
        pa, pb = Path(config.CORPUS_ROOT) / a, Path(config.CORPUS_ROOT) / b
        if not (pa.exists() and pb.exists()):
            print(f"  MISS  源件缺失  {b}")
            pair_miss += 1
            continue
        try:
            fa, fb = pix_fp(pa, with_meta=True), pix_fp(pb, with_meta=True)
            same = fa["fp"] == fb["fp"] and fa["page_count"] == fb["page_count"]
        except Exception as ex:  # noqa: BLE001 —— 渲染失败记 MISS，不算退化
            print(f"  MISS  渲染失败 {type(ex).__name__}  {Path(b).name}")
            pair_miss += 1
            continue
        pair_checked += 1
        flag = same == (want == "同件")
        pair_fail += 0 if flag else 1
        print(f"  {'OK  ' if flag else 'FAIL'} 期望{want} 实际{'同件' if same else '不同件'}  {Path(a).name}")

    checked = (len(CASES) - miss) + pair_checked
    missing = miss + pair_miss
    miss_rate = missing / max(checked + missing, 1)
    regress = (len(CASES) - miss - passed) + pair_fail
    if checked == 0 or miss_rate > SENTINEL_MISS_MAX:
        print(f"\n[SENTINEL] ✗ 防护触发：实际检查 {checked} 项，MISS {missing} 项"
              f"（{miss_rate:.1%}），无法判定 → 失败（阈值 {SENTINEL_MISS_MAX:.0%}）")
        return False
    print(f"\n[SENTINEL] 单件 {passed}/{len(CASES) - miss}（MISS {miss} 不计退化）"
          f" + 重复对 {pair_checked}/{len(PAIRS)}（MISS {pair_miss}） → "
          + ("全绿" if regress == 0 else f"退化 {regress} 处，先查再跑"))
    return regress == 0


def scan_dir(md_dir: Path) -> dict:
    """独立门禁：扫描任意 md 目录。rows.rel = md 相对 md_dir 的 posix 路径（无源回链）。"""
    files = sorted(md_dir.rglob("*.md"))
    rows = []
    for p in files:
        raw, enc = read_text_info(p)
        m = metrics(raw)
        state, why = judge(m)
        try:
            md_hash = hashing.sha256_file(p)
        except OSError:
            md_hash = None
        rows.append(dict(rel=p.relative_to(md_dir).as_posix(), md=str(p), doc_hash=None,
                         exists=True, drift=False, md_hash=md_hash, state=state, reason=why,
                         chars=m["chars"], cjk=round(m["cjk"], 4), latin=round(m["latin"], 4),
                         bad=round(m["bad"], 4), fixed=m["fixed"], nfkc_changed=m["nfkc_changed"],
                         encoding=enc, head=head_of(raw)))
    sp = [r for r in rows if r["state"] == "pass"]
    sr = [r for r in rows if r["state"] == "review"]
    sf = [r for r in rows if r["state"] == "fail"]
    return {
        "summary": dict(md_dir=str(md_dir), files=len(rows), passed=len(sp),
                        review=len(sr), failed=len(sf),
                        abnormal_rate=round((len(sr) + len(sf)) / max(len(rows), 1), 4),
                        collisions=0, real_collisions=0, drift=0),
        "thresholds": dict(bad=TH_BAD, cjk=TH_CJK, min_chars=TH_MIN, nfkc=True),
        "rows": rows,
        "collisions": [],
    }


def cross_check(raw: str) -> dict:
    """低成本交叉校验：同文在 gate 三态指标与 textmetrics 四态指标上的偏差。

    两套口径有意独立（报告 vs 台账），但 CJK 占比/可见长度不应漂移到无法解释。
    """
    g = metrics(raw)
    clean = textmetrics.strip_markdown_noise(raw)
    t = textmetrics.metrics(clean)
    return {
        "cjk_delta": round(abs(g["cjk"] - t["cjk_ratio"]), 4),
        "visible_delta": abs(g["chars"] - t["visible_chars"]),
        "bad_vs_garbled": round(abs(g["bad"] - t["garbled_ratio"]), 4),
    }


def gate_file_ledger(path: Path) -> dict:
    """gate_results 台账口径：textmetrics 四态（schema 001 verdict 注释口径）。"""
    raw, enc = read_text_info(path)
    clean = textmetrics.strip_markdown_noise(raw)
    m = textmetrics.metrics(clean)
    v = textmetrics.verdict(
        clean,
        cjk_min=config.GATE_CJK_MIN,
        garbled_max=config.GATE_GARBLED_MAX,
        valid_min=config.GATE_VALID_MIN,
        min_visible=config.GATE_MIN_VISIBLE,
    )
    return {**m, "verdict": v, "md_path": str(path), "encoding": enc,
            "cross_check": cross_check(raw)}


def _ledger_verdict(r: dict) -> str:
    if r["state"] == "pass":
        return "pass"
    if r["state"] == "review":
        return "low_cjk"
    if "过短" in (r.get("reason") or "") or "缺失" in (r.get("reason") or ""):
        return "non_content"
    return "garbled"


def _write_audit_ledger(conn, rows: list[dict]) -> None:
    """--audit 行写 gate_results（带 doc_hash + md_hash），并把三态结论回写 source_documents。"""
    now = datetime.now().strftime(NOW_FMT)
    has_md_hash = any(r[1] == "md_hash" for r in conn.execute("PRAGMA table_info(gate_results)"))
    current_hashes, current_mds = set(), set()
    for r in rows:
        dh = r.get("doc_hash")
        if dh:
            current_hashes.add(dh)
            # 缺失产物也要回写 source_documents：T30 才能把“产物缺失”挡在抽题前
            conn.execute("UPDATE source_documents SET gate_result=? WHERE doc_hash=?",
                         (_ledger_verdict(r), dh))
        if not r.get("exists"):
            continue
        current_mds.add(path_key(r["md"]))
        vis = int(r.get("chars") or 0)
        cjk_ratio = float(r.get("cjk") or 0)
        bad = float(r.get("bad") or 0)
        if has_md_hash:
            conn.execute(
                "INSERT OR REPLACE INTO gate_results "
                "(md_path, doc_hash, visible_chars, cjk_chars, cjk_ratio, garbled_ratio, "
                "valid_ratio, verdict, checked_at, md_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["md"], dh, vis, round(cjk_ratio * vis), round(cjk_ratio, 4),
                 round(bad, 4), round(max(0.0, 1.0 - bad), 4), _ledger_verdict(r), now,
                 r.get("md_hash")))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO gate_results "
                "(md_path, doc_hash, visible_chars, cjk_chars, cjk_ratio, garbled_ratio, "
                "valid_ratio, verdict, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["md"], dh, vis, round(cjk_ratio * vis), round(cjk_ratio, 4),
                 round(bad, 4), round(max(0.0, 1.0 - bad), 4), _ledger_verdict(r), now))

    # 只清理本次 audit 覆盖到的 doc_hash 的旧路径，避免误删 --md-dir 台账
    for old in conn.execute("SELECT md_path, doc_hash FROM gate_results "
                            "WHERE doc_hash IS NOT NULL").fetchall():
        if old["doc_hash"] in current_hashes and path_key(old["md_path"]) not in current_mds:
            conn.execute("DELETE FROM gate_results WHERE md_path=?", (old["md_path"],))


def _write_md_dir_ledger(conn, md_dir: Path) -> None:
    now = datetime.now().strftime(NOW_FMT)
    current = set()
    for p in sorted(md_dir.rglob("*.md")):
        r = gate_file_ledger(p)
        current.add(path_key(r["md_path"]))
        conn.execute(
            "INSERT OR REPLACE INTO gate_results "
            "(md_path, doc_hash, visible_chars, cjk_chars, cjk_ratio, garbled_ratio, "
            "valid_ratio, verdict, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["md_path"], None, r["visible_chars"], r["cjk_chars"], r["cjk_ratio"],
             r["garbled_ratio"], r["valid_ratio"], r["verdict"], now))
    prefix = str(md_dir) + os.sep
    for old in conn.execute("SELECT md_path FROM gate_results "
                            "WHERE doc_hash IS NULL AND md_path LIKE ?",
                            (prefix + "%",)).fetchall():
        if path_key(old["md_path"]) not in current:
            conn.execute("DELETE FROM gate_results WHERE md_path=?", (old["md_path"],))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="md 产物文本质量门禁（三态 pass/review/fail）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--md-dir", default=None, help="扫描任意 md 目录（独立门禁）")
    mode.add_argument("--audit", action="store_true",
                      help="生产口径：按 BUILD/convert_log.json ok 件驱动，产 dispatch/ocr_merge 兼容报告")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--no-db", action="store_true",
                    help="不写 gate_results 台账 / 不回写 source_documents.gate_result")
    ap.add_argument("--out", default=None,
                    help="报告 JSON 落盘路径（--audit 缺省写 BUILD/gate_report.json）")
    ap.add_argument("--sentinel", action="store_true",
                    help="哨兵回归集：13 单件 + 2 重复对判定不退化校验（读生产语料，不写报告）")
    ap.add_argument("--top", type=int, default=15, help="打印最差的 N 个")
    ap.add_argument("--strict", action="store_true",
                    help="严格退出码：真覆盖>0 或异常率>--max-abnormal 时返回 1")
    ap.add_argument("--max-abnormal", type=float, default=0.10,
                    help="--strict 下的异常率上限（默认 0.10）")
    args = ap.parse_args(argv)

    if args.sentinel:
        if args.audit or args.md_dir:
            ap.error("--sentinel 不能与 --audit/--md-dir 同用")
        return 0 if run_sentinel() else 1

    if not args.audit and not args.md_dir:
        ap.error("需要 --audit 或 --md-dir 之一")

    if args.audit:
        report = run_audit()
    else:
        md_dir = Path(args.md_dir)
        if not md_dir.exists():
            print(f"[fatal] 目录不存在: {md_dir}", file=sys.stderr)
            return 2
        report = scan_dir(md_dir)

    # 先落报告：即使随后写库被锁/失败，扫描结果也不丢
    out_path = (Path(args.out) if args.out
                else (Path(config.BUILD_DIR) / "gate_report.json" if args.audit else None))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.no_db:
        db_path = Path(args.db)
        try:
            with wlock.write_lock("kb", tool="gate", build_dir=db_path.parent, target=db_path):
                conn = db.connect(args.db)
                try:
                    if args.audit:
                        _write_audit_ledger(conn, report["rows"])
                    else:
                        _write_md_dir_ledger(conn, Path(args.md_dir))
                    conn.commit()
                finally:
                    conn.close()
        except wlock.LockHeld as ex:
            print(json.dumps({"verdict": "ABORT", "why": str(ex),
                              "note": "另有工具在写 kb.sqlite；gate 台账未写入，但报告已落盘"},
                             ensure_ascii=False, indent=1))
            return 2

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    bad = [r for r in report["rows"] if r["state"] != "pass"]
    if bad:
        print(f"\n--- 最差 {min(args.top, len(bad))} 个 ---")
        bad.sort(key=lambda r: (r.get("cjk", 0.0), -(r.get("chars") or 0)))
        for r in bad[: args.top]:
            name = Path(r["md"]).name
            print(f"{r['state']:<7} CJK={r.get('cjk', 0.0):.3f} bad={r.get('bad', 0.0):.3f} "
                  f"字数={r.get('chars', 0):>7} | {name[:70]}")
    if out_path is not None:
        print(f"[out] {out_path}")

    if args.strict:
        abnormal = report["summary"].get("abnormal_rate", 0.0)
        real = report["summary"].get("real_collisions", 0)
        if real > 0 or abnormal > args.max_abnormal:
            print(f"[strict] 失败：real_collisions={real} abnormal_rate={abnormal:.4f} "
                  f"（上限 {args.max_abnormal:.2f}）", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
