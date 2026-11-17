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
TH_BAD = 0.02     # 异常字符比上限（>= 判 fail）
TH_CJK = 0.15      # CJK 比例下限（< 判 review）
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

    idx: dict[str, list[str]] = {}
    for root, _dn, files in os.walk(config.QUESTION_MD_DIR):
        for fn in files:
            if fn.lower().endswith(".md"):
                idx.setdefault(fn, []).append(os.path.join(root, fn))

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
    ap.add_argument("--top", type=int, default=15, help="打印最差的 N 个")
    args = ap.parse_args(argv)

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
