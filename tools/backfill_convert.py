# -*- coding: utf-8 -*-
"""backfill_convert：G1/G2 缺口补录驱动（356 篇"classified 但无 md"）。

链路：台账圈定缺口 → 复用语料侧 convert_one.py 转 md → 低CJK/崩溃的进 OCR 清单
     → 复用 ocr_pages.py --batch 跑 OCR → 把 OCR 全文落成 md（补跑转换的下半段）。

判定"缺口"的口径（与 docs/15 G1/G2 一致）：
  1) source_documents.status='classified' 且源文件仍在
  2) 20-题库MD 里找不到同名 md（按 stem 归一化：去前导序号）
  3) 该源 doc_hash 与匹配到的 md-mirror hash 在 questions 里都没有题

用法：
    python tools/backfill_convert.py --plan                 # 圈缺口，写 build/backfill_a.json
    python tools/backfill_convert.py --convert --limit 5    # 先转 5 篇验证
    python tools/backfill_convert.py --convert              # 全量（断点续跑）
    python tools/backfill_convert.py --convert --only-pdf   # 只转 PDF
    python tools/backfill_convert.py --ocr-list             # 导出 OCR 清单
    python tools/backfill_convert.py --merge-ocr            # 把已有 OCR 全文落成 md
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config  # noqa: E402

BUILD = Path(config.BUILD_DIR)
LEDGER = Path(config.DB_PATH)
SEARCH_DB = BUILD / "面经库.db"
MD_ROOT = Path(config.QUESTION_MD_DIR)
CORPUS = Path(config.CORPUS_ROOT)
CONVERTER = CORPUS / "21-面经数据库" / "tools" / "convert_one.py"

PLAN_FILE = BUILD / "backfill_a.json"
CONVERT_LOG = BUILD / "backfill_convert_log.json"
OCR_LIST_FILE = BUILD / "ocr_needed_backfill.json"

CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经", "个人面经": "个人面经", "专题笔记": "专题笔记"}
NOW = lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731


def _stem(name: str) -> str:
    s = Path(name).stem.lower()
    return re.sub(r"^\d+[-_.\s]*", "", s)


def _search_hashes() -> set[str]:
    """检索库里有题的 doc_hash 集合（路径 hash 口径，与 extract 一致）。"""
    import sqlite3 as s3
    if not SEARCH_DB.exists():
        return set()
    con = s3.connect(f"file:{SEARCH_DB.as_posix()}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT doc_hash FROM questions")}
    finally:
        con.close()


def build_plan() -> list[dict]:
    md_by_stem: dict[str, list[str]] = {}
    if MD_ROOT.exists():
        for p in MD_ROOT.rglob("*.md"):
            rel = p.relative_to(MD_ROOT).as_posix()
            md_by_stem.setdefault(_stem(p.name), []).append(rel)
    qs = _search_hashes()
    import hashlib
    led = sqlite3.connect(f"file:{LEDGER.as_posix()}?mode=ro", uri=True)
    led.row_factory = sqlite3.Row
    rows = led.execute(
        "SELECT doc_hash, file_name, ext, category, topic, src_path "
        "FROM source_documents WHERE status='classified'").fetchall()
    led.close()

    plan, skip = [], []
    for r in rows:
        src = Path(r["src_path"])
        if not src.exists():
            skip.append({"doc_hash": r["doc_hash"], "file_name": r["file_name"], "why": "src_missing"})
            continue
        if r["doc_hash"] in qs:
            continue
        cands = md_by_stem.get(_stem(r["file_name"]), [])
        for rel in cands:
            h = hashlib.sha256(("md-mirror:" + rel).encode()).hexdigest()[:16]
            if h in qs:
                skip.append({"doc_hash": r["doc_hash"], "file_name": r["file_name"], "why": "md_mirror_has_questions", "md": rel})
                break
        else:
            try:
                rel_src = src.relative_to(CORPUS).as_posix()
            except ValueError:
                skip.append({"doc_hash": r["doc_hash"], "file_name": r["file_name"], "why": "outside_corpus"})
                continue
            cat = CAT_MAP.get(r["category"] or "") or ("面经" if "面经" in (r["category"] or "") else "题库")
            plan.append({
                "doc_hash": r["doc_hash"], "rel": rel_src, "title": r["file_name"],
                "ext": (r["ext"] or src.suffix), "category": cat,
                "topic": r["topic"] or "综合", "src_path": str(src),
                "size": src.stat().st_size,
            })
    plan.sort(key=lambda x: (-x["size"]))
    PLAN_FILE.write_text(json.dumps({"generated_at": NOW(), "plan": plan, "skip": skip},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    by_ext: dict[str, int] = {}
    for p in plan:
        by_ext[p["ext"]] = by_ext.get(p["ext"], 0) + 1
    print(json.dumps({"缺口件数": len(plan), "按扩展名": by_ext, "跳过": len(skip)}, ensure_ascii=False))
    for s in skip[:5]:
        print("  skip:", s)
    return plan


def _run_one(entry: dict, extractor: str = "pymupdf4llm") -> dict | None:
    tmpd = Path(tempfile.mkdtemp(prefix="bf_"))
    ej, rj = tmpd / "e.json", tmpd / "r.json"
    ej.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    try:
        p = subprocess.run([sys.executable, "-I", str(CONVERTER), str(ej), str(rj),
                            "--extractor", extractor],
                           capture_output=True, timeout=300, cwd=str(tempfile.gettempdir()))
        if rj.exists():
            return json.loads(rj.read_text(encoding="utf-8"))
        return {"rel": entry["rel"], "ok": False,
                "err": f"no_result rc={p.returncode} {(p.stderr or b'')[-200:]!r}"}
    except subprocess.TimeoutExpired:
        return {"rel": entry["rel"], "ok": False, "err": "timeout300"}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _target_md(entry: dict) -> Path:
    """convert_one 的落盘路径：<mdroot>/<category>/<topic>/<stem>.md"""
    cat = CAT_MAP.get(entry.get("category") or "", "题库")
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", Path(entry["title"]).stem).strip()[:80]
    return MD_ROOT / cat / (entry.get("topic") or "综合") / f"{stem}.md"


def _hash_suffix(entry: dict) -> str:
    """md 文件名后缀：doc_hash 前 8 位（同名件互不覆盖；extract 按路径算 hash 仍然稳定）。"""
    h = entry.get("doc_hash") or ""
    return h[:8] or "nohash"


def _rename_to_hashed(entry: dict) -> Path | None:
    """把 convert_one 刚写出的 <stem>.md 改名成 <stem>__<hash8>.md，避免同名覆盖。"""
    t = _target_md(entry)
    if not t.exists():
        return None
    new = t.with_name(f"{t.stem}__{_hash_suffix(entry)}{t.suffix}")
    try:
        if new.exists():
            new.unlink()
        t.rename(new)
        return new
    except OSError:
        return t


def rehash_existing() -> None:
    """给已转好但还没带 hash 后缀的 md 补改名（断点续跑前的一次性修正）。"""
    log = json.loads(CONVERT_LOG.read_text(encoding="utf-8")) if CONVERT_LOG.exists() else []
    plan = {p["rel"]: p for p in json.loads(PLAN_FILE.read_text(encoding="utf-8"))["plan"]}
    moved = 0
    for rec in log:
        if not rec.get("ok"):
            continue
        e = plan.get(rec["rel"])
        if not e:
            continue
        got = _rename_to_hashed(e)
        if got and got.name != _target_md(e).name:
            moved += 1
    print(json.dumps({"rehashed": moved}, ensure_ascii=False))


def convert(limit: int = 0, only_pdf: bool = False) -> None:
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))["plan"] if PLAN_FILE.exists() else build_plan()
    log = json.loads(CONVERT_LOG.read_text(encoding="utf-8")) if CONVERT_LOG.exists() else []
    done = {l["rel"] for l in log if l.get("ok")}
    todo = [e for e in plan if e["rel"] not in done]
    if only_pdf:
        todo = [e for e in todo if e["ext"].lower() == ".pdf"]
    if limit:
        todo = todo[:limit]
    ocr: list[dict] = []
    t0 = time.perf_counter()
    ok = fail = 0
    for i, e in enumerate(todo):
        # 转换前：目标 md 若已存在（同名件先写过），先按 hash 后缀改名收走，杜绝覆盖
        tgt = _target_md(e)
        if tgt.exists():
            bak = tgt.with_name(f"{tgt.stem}__pre{tgt.suffix}")
            try:
                if bak.exists():
                    bak.unlink()
                tgt.rename(bak)
            except OSError:
                pass
        r = _run_one(e)
        if r is None or not r.get("ok") and r.get("extractor") == "pymupdf4llm":
            r2 = _run_one(e, "pypdf")
            r = r2 if (r2 and r2.get("ok")) else (r or r2 or {})
        md_hashed = _rename_to_hashed(e) if r and r.get("ok") else None
        rec = {"rel": e["rel"], "doc_hash": e["doc_hash"], "at": NOW(),
               "md": str(md_hashed.relative_to(MD_ROOT).as_posix()) if md_hashed else None,
               **(r or {})}
        log = [l for l in log if l["rel"] != e["rel"]] + [rec]
        if rec.get("ok"):
            ok += 1
        else:
            fail += 1
            ocr.append({"rel": e["rel"], "file_name": e["title"], "why": rec.get("err")})
        CONVERT_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{i+1}/{len(todo)}] {'OK' if rec.get('ok') else 'FAIL'} {e['rel']} "
              f"{(rec.get('chars') or '')} {rec.get('err') or ''}", flush=True)
        if (i + 1) % 20 == 0:
            OCR_LIST_FILE.write_text(json.dumps(ocr, ensure_ascii=False, indent=1), encoding="utf-8")
    OCR_LIST_FILE.write_text(json.dumps(ocr, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": ok, "fail": fail, "ocr_needed": len(ocr),
                      "elapsed_sec": round(time.perf_counter() - t0, 1)}, ensure_ascii=False))


def write_ocr_list() -> None:
    log = json.loads(CONVERT_LOG.read_text(encoding="utf-8")) if CONVERT_LOG.exists() else []
    ocr = [{"rel": l["rel"], "file_name": Path(l["rel"]).name, "why": l.get("err")}
           for l in log if not l.get("ok")]
    OCR_LIST_FILE.write_text(json.dumps([o["rel"] for o in ocr], ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(json.dumps({"ocr_list": str(OCR_LIST_FILE), "count": len(ocr)}, ensure_ascii=False))


def merge_ocr() -> None:
    """把 build/ocr_text/<rel>.txt 落成 20-题库MD/<category>/<topic>/<stem>.md（无 md 的件）。"""
    plan = {p["rel"]: p for p in json.loads(PLAN_FILE.read_text(encoding="utf-8"))["plan"]} \
        if PLAN_FILE.exists() else {}
    ocr_root = BUILD / "ocr_text"
    made, skipped = 0, 0
    for txt in sorted(ocr_root.rglob("*.txt")):
        rel = txt.relative_to(ocr_root).as_posix()
        src_rel = rel[:-4] if rel.endswith(".txt") else rel   # 去掉 ".pdf.txt" 里的 .txt
        src_rel = re.sub(r"\.(pdf|docx|doc|pptx|rtf|txt|md)$", lambda m: m.group(1), src_rel, flags=re.I)
        stem = Path(src_rel).stem
        cand = MD_ROOT.rglob(f"{stem}.md")
        if any(cand):
            skipped += 1
            continue
        p = plan.get(src_rel) or plan.get(rel)
        cat = (p or {}).get("category", "题库")
        topic = (p or {}).get("topic", "综合")
        out_dir = MD_ROOT / CAT_MAP.get(cat, "题库") / (topic or "综合")
        out_dir.mkdir(parents=True, exist_ok=True)
        body = txt.read_text(encoding="utf-8", errors="replace")
        (out_dir / f"{stem}.md").write_text(body, encoding="utf-8")
        made += 1
    print(json.dumps({"new_md": made, "skipped_md_exists": skipped}, ensure_ascii=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="G1/G2 缺口补录驱动")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--convert", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-pdf", action="store_true")
    ap.add_argument("--ocr-list", action="store_true")
    ap.add_argument("--merge-ocr", action="store_true")
    ap.add_argument("--rehash", action="store_true", help="给已完成但没带 hash 后缀的 md 补改名")
    args = ap.parse_args(argv)
    if args.plan:
        build_plan()
    if args.convert:
        convert(args.limit, args.only_pdf)
    if args.rehash:
        rehash_existing()
    if args.ocr_list:
        write_ocr_list()
    if args.merge_ocr:
        merge_ocr()
    if not any(vars(args).values()):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
