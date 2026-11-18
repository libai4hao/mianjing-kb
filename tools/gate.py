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

判据（先 NFKC 归一，再算指标）：
    字符数 < 100        -> fail    极短产物
    异常字符比 >= 2%     -> fail    真乱码（ړ ୗ ᭗ 这类非中文码位）
    CJK 比例 < 15%      -> review  低中文占比，人工/OCR 裁定（英文书与乱码都落这里，不硬判）
    其余                -> pass

为什么必须 NFKC：PDF 提取常把汉字映射进 Kangxi 部首区（U+2F00），"大厂" 会变成 "⼤厂"。
内容可读，但检索、去重、聚合全部失效。归一后健康件异常率 4% 降到 0.2%。

gate_results 台账（--md-dir 模式，--no-db 跳过）仍用 textmetrics 四态口径
（schema 001 的 verdict 注释：pass|low_cjk|garbled|non_content），与三态报告各司其职：
台账回答"这篇 md 的文本质量"，报告回答"哪些件要分流"。

用法：
    python tools/gate.py --audit                          # 生产：产 BUILD/gate_report.json
    python tools/gate.py --md-dir "<CORPUS_ROOT>\\20-题库MD" --top 20
    python tools/gate.py --md-dir ... --no-db --out report.json
    python tools/gate.py --sentinel                       # 哨兵回归集：判定不退化校验（语料侧移植）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db, hashing, textmetrics  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"

# 三态阈值（对齐语料侧 gate.py，2026-11-17 审核后统一到本仓库）
# F10（2026-11-19）口径说明：TH_CJK 与 config.GATE_CJK_MIN 同义，单源引用；
# TH_BAD/TH_MIN 与 config 的 GATE_GARBLED_MAX/GATE_MIN_VISIBLE 是**不同指标**
# （三态报告 vs 四态台账，见 docstring 上方"各司其职"），有意不合并数值。
TH_BAD = 0.02     # 异常字符比上限（>= 判 fail）
TH_CJK = config.GATE_CJK_MIN   # CJK 比例下限（< 判 review），与 config 单源
TH_MIN = 100       # 最少字符数（< 判 fail）

CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经",
           "个人面经": "个人面经", "专题笔记": "专题笔记"}
ILLEGAL = r'[\\/:*?"<>|\r\n\t]'


def read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="ignore")


def safe(s: str) -> str:
    return re.sub(ILLEGAL, "_", s or "").strip()[:80]


def metrics(raw: str) -> dict:
    """NFKC 归一后算三态指标：chars / cjk / latin / bad（均为比例），fixed = NFKC 改写的字符数。"""
    text = unicodedata.normalize("NFKC", raw)
    fixed = -1
    if len(raw) == len(text):
        fixed = sum(1 for a, b in zip(raw, text) if a != b)
    n = len(text)
    if n == 0:
        return dict(chars=0, cjk=0.0, latin=0.0, bad=1.0, fixed=0)
    cjk = latin = bad = 0
    for c in text:
        o = ord(c)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
            continue
        if o < 0x80:
            if c.isalpha():
                latin += 1
            continue
        if 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:   # CJK 标点 / 全角形式
            continue
        if 0x2000 <= o <= 0x27BF or 0x00A0 <= o <= 0x024F:   # 通用标点 / Latin 扩展
            continue
        if o in (0xFE0F, 0x200B, 0x200D, 0xFEFF):            # 变体选择符 / 零宽字符
            continue
        bad += 1
    return dict(chars=n, cjk=cjk / n, latin=latin / n, bad=bad / n, fixed=fixed)


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
    """registry 行 → 约定产物路径：<QUESTION_MD_DIR>/<category>/<topic>/<stem>.md"""
    cat = CAT_MAP.get(e.get("category"), "综合")
    topic = safe(e.get("topic")) or "综合"
    stem = safe(os.path.splitext(e.get("title") or e["rel"])[0])
    return Path(config.QUESTION_MD_DIR) / cat / topic / (stem + ".md")


def md_index() -> dict[str, list[str]]:
    """20-题库MD 全量 basename 索引（--audit 漂移解析与 --sentinel 共用）。"""
    idx: dict[str, list[str]] = {}
    for root, _dn, files in os.walk(config.QUESTION_MD_DIR):
        for fn in files:
            if fn.lower().endswith(".md"):
                idx.setdefault(fn, []).append(os.path.join(root, fn))
    return idx


def collisions_of(rows: list[dict]) -> list[dict]:
    """同一 md 被多个源写入：md5 内容比对区分 同内容重复 / 真覆盖(丢内容)。"""
    paths: dict[str, list[str]] = {}
    for r in rows:
        paths.setdefault(os.path.normcase(r["md"]), []).append(r["rel"])
    out = []
    for p, rels in paths.items():
        if len(rels) < 2:
            continue
        sig = set()
        for rel in rels:
            try:
                sig.add(hashlib.md5((Path(config.CORPUS_ROOT) / rel).read_bytes()).hexdigest())
            except OSError:
                sig.add("err:" + rel)
        kind = "同内容重复" if len(sig) == 1 else "真覆盖(丢内容)"
        out.append(dict(md=p, rels=rels, kind=kind))
    return out


def run_audit() -> dict:
    """生产口径：convert_log ok 件 → 逐件找产物 md → 三态判定 → rows + collisions。

    输出结构与 dispatch / ocr_merge 消费的 gate_report.json 完全兼容；
    在语料侧版本基础上补 doc_hash（源文件 sha256 前 16 位，P2-4 修复：
    原 --md-dir 台账的 doc_hash 恒 NULL，产物与源非一一对应无法可靠回链；
    --audit 模式源路径已知，直接算真值）。
    """
    build = Path(config.BUILD_DIR)
    reg = json.loads((build / "registry.json").read_text(encoding="utf-8"))
    log = json.loads((build / "convert_log.json").read_text(encoding="utf-8"))
    by_rel = {e["rel"]: e for e in reg}
    ok = [l for l in log if l.get("ok")]

    idx = md_index()

    rows = []
    for l in ok:
        e = by_rel.get(l["rel"])
        if not e:
            continue
        p = md_path_for(e)
        drift = False
        if not p.exists():
            cand = idx.get(p.name) or []
            if len(cand) == 1:                     # 转换后被移动/重分类：唯一同名命中即采用
                p = Path(cand[0])
                drift = True
        src = Path(config.CORPUS_ROOT) / l["rel"]
        doc_hash = hashing.sha256_file(src)[:16] if src.exists() else None
        if not p.exists():
            rows.append(dict(rel=l["rel"], md=str(p), doc_hash=doc_hash, exists=False,
                             state="fail", reason="产物缺失", drift=False))
            continue
        raw = read_text(p)
        m = metrics(raw)
        state, why = judge(m)
        rows.append(dict(rel=l["rel"], md=str(p), doc_hash=doc_hash, exists=True, drift=drift,
                         state=state, reason=why, chars=m["chars"], cjk=round(m["cjk"], 4),
                         latin=round(m["latin"], 4), bad=round(m["bad"], 4),
                         fixed=m["fixed"], head=head_of(raw)))

    colls = collisions_of(rows)
    sp = [r for r in rows if r["state"] == "pass"]
    sr = [r for r in rows if r["state"] == "review"]
    sf = [r for r in rows if r["state"] == "fail"]
    real = sum(1 for c in colls if c["kind"].startswith("真"))
    drift = sum(1 for r in rows if r.get("drift"))
    rate = (len(sf) + len(sr)) / max(len(ok), 1)
    return {
        "summary": dict(converted=len(ok), passed=len(sp), review=len(sr), failed=len(sf),
                        abnormal_rate=round(rate, 4), collisions=len(colls),
                        real_collisions=real, drift=drift),
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


def pix_fp(pdf_path: Path, dpi: int = 72) -> str:
    """PDF 像素指纹：采样 10%/35%/60%/85% 四页渲染 md5 拼接（语料侧同款）。"""
    import pymupdf
    with pymupdf.open(str(pdf_path)) as doc:
        n = doc.page_count
        picks = sorted({min(int(n * r), n - 1) for r in (0.1, 0.35, 0.6, 0.85)})
        return "".join(hashlib.md5(doc[i].get_pixmap(dpi=dpi).samples).hexdigest()[:8]
                       for i in picks)


def resolve_md(e: dict, idx: dict[str, list[str]]) -> Path | None:
    """registry 行 → 产物 md：canonical → 同名漂移（唯一命中）→ <stem>__<hash8> 变体 → None。"""
    p = md_path_for(e)
    if p.exists():
        return p
    cands = idx.get(p.name) or []
    if len(cands) == 1:
        return Path(cands[0])
    variants = sorted(p.parent.glob(p.stem + "__*.md")) if p.parent.exists() else []
    if variants:
        return variants[0]
    return None


def run_sentinel() -> bool:
    """跑哨兵：判定与期望不符（退化）→ False；registry 缺行 / 源件缺失记 MISS 不计退化。"""
    build = Path(config.BUILD_DIR)
    reg = json.loads((build / "registry.json").read_text(encoding="utf-8"))
    by_rel = {e["rel"]: e for e in reg}
    idx = md_index()
    passed = miss = 0
    print(f"-- 单件判定哨兵（{len(CASES)} 件）--")
    for rel, want in CASES:
        e = by_rel.get(rel)
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
    pair_fail = pair_miss = 0
    for want, a, b in PAIRS:
        pa, pb = Path(config.CORPUS_ROOT) / a, Path(config.CORPUS_ROOT) / b
        if not (pa.exists() and pb.exists()):
            print(f"  MISS  源件缺失  {b}")
            pair_miss += 1
            continue
        try:
            same = pix_fp(pa) == pix_fp(pb)
        except Exception as ex:  # noqa: BLE001 —— 渲染失败记 MISS，不算退化
            print(f"  MISS  渲染失败 {type(ex).__name__}  {Path(b).name}")
            pair_miss += 1
            continue
        flag = same == (want == "同件")
        pair_fail += 0 if flag else 1
        print(f"  {'OK  ' if flag else 'FAIL'} 期望{want} 实际{'同件' if same else '不同件'}  {Path(a).name}")
    regress = (len(CASES) - miss - passed) + pair_fail
    print(f"\n[SENTINEL] 单件 {passed}/{len(CASES) - miss}（MISS {miss} 不计退化）"
          f" + 重复对 {len(PAIRS) - pair_miss}/{len(PAIRS)} → "
          + ("全绿" if regress == 0 else f"退化 {regress} 处，先查再跑"))
    return regress == 0


def scan_dir(md_dir: Path) -> dict:
    """独立门禁：扫描任意 md 目录。rows.rel = md 相对 md_dir 的 posix 路径（无源回链）。"""
    files = sorted(md_dir.rglob("*.md"))
    rows = []
    for p in files:
        raw = read_text(p)
        m = metrics(raw)
        state, why = judge(m)
        rows.append(dict(rel=p.relative_to(md_dir).as_posix(), md=str(p), doc_hash=None,
                         exists=True, drift=False, state=state, reason=why,
                         chars=m["chars"], cjk=round(m["cjk"], 4), latin=round(m["latin"], 4),
                         bad=round(m["bad"], 4), fixed=m["fixed"], head=head_of(raw)))
    sp = [r for r in rows if r["state"] == "pass"]
    sr = [r for r in rows if r["state"] == "review"]
    sf = [r for r in rows if r["state"] == "fail"]
    return {
        "summary": dict(md_dir=str(md_dir), files=len(rows), passed=len(sp),
                        review=len(sr), failed=len(sf)),
        "thresholds": dict(bad=TH_BAD, cjk=TH_CJK, min_chars=TH_MIN, nfkc=True),
        "rows": rows,
        "collisions": [],
    }


def gate_file_ledger(path: Path) -> dict:
    """gate_results 台账口径：textmetrics 四态（schema 001 verdict 注释口径）。

    doc_hash 保持 NULL：--md-dir 模式下 md 产物与源文件不是一一对应
    （schema 001 注释原话），无法可靠回链；源级真值见 --audit 报告的 rows.doc_hash。
    """
    raw = read_text(path)
    clean = textmetrics.strip_markdown_noise(raw)
    m = textmetrics.metrics(clean)
    v = textmetrics.verdict(
        clean,
        cjk_min=config.GATE_CJK_MIN,
        garbled_max=config.GATE_GARBLED_MAX,
        valid_min=config.GATE_VALID_MIN,
        min_visible=config.GATE_MIN_VISIBLE,
    )
    return {**m, "verdict": v, "md_path": str(path)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="md 产物文本质量门禁（三态 pass/review/fail）")
    ap.add_argument("--md-dir", default=None, help="扫描任意 md 目录（独立门禁）")
    ap.add_argument("--audit", action="store_true",
                    help="生产口径：按 BUILD/convert_log.json ok 件驱动，产 dispatch/ocr_merge 兼容报告")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--no-db", action="store_true", help="--md-dir 模式下不写 gate_results 台账")
    ap.add_argument("--out", default=None,
                    help="报告 JSON 落盘路径（--audit 缺省写 BUILD/gate_report.json）")
    ap.add_argument("--sentinel", action="store_true",
                    help="哨兵回归集：13 单件 + 2 重复对判定不退化校验（读生产语料，不写报告）")
    ap.add_argument("--top", type=int, default=15, help="打印最差的 N 个")
    args = ap.parse_args(argv)

    if args.sentinel:
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
        if not args.no_db:
            conn = db.connect(args.db)
            for p in sorted(md_dir.rglob("*.md")):
                r = gate_file_ledger(p)
                conn.execute(
                    "INSERT OR REPLACE INTO gate_results "
                    "(md_path, doc_hash, visible_chars, cjk_chars, cjk_ratio, garbled_ratio, valid_ratio, verdict, checked_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["md_path"], None, r["visible_chars"], r["cjk_chars"], r["cjk_ratio"],
                     r["garbled_ratio"], r["valid_ratio"], r["verdict"], datetime.now().strftime(NOW_FMT)),
                )
            conn.commit()
            conn.close()

    out_path = (Path(args.out) if args.out
                else (Path(config.BUILD_DIR) / "gate_report.json" if args.audit else None))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
