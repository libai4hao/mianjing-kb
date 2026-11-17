# -*- coding: utf-8 -*-
"""P2：同名覆盖重转——convert_log 里被"后转覆盖先转"顶掉的 26 条 + 指定补转件。

- 输出名带 doc_hash 后缀（sha256[:8]），杜绝再次同名覆盖
- 复用存量转换器 21-面经数据库\\tools\\convert_one.py（pymupdf4llm 主路 + 图片抽取）
- 源件已不存在/隔离的跳过并记因；结果写 build/reconvert_log.json

用法：
    python tools/reconvert.py --dry-run     # 只算清单不落盘
    python tools/reconvert.py               # 实际重转
    python tools/reconvert.py --extra "31-收件箱/..."   # 追加补转件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(r"<CORPUS_ROOT>\21-面经数据库\tools")))

import convert_one  # noqa: E402  存量转换器（pdf_to_md / docx_to_md / safe_name / cjk_ratio）
from kb import config, db  # noqa: E402

BUILD = Path(config.BUILD_DIR)             # P1-3 修复（2026-11-17 审核）：路径统一走 config
MD_ROOT = Path(config.QUESTION_MD_DIR)     # KB_* 环境变量可覆盖，换机器不再失效
CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经", "个人面经": "个人面经", "专题笔记": "专题笔记"}


def losers_from_log() -> list[dict]:
    log = json.loads((BUILD / "convert_log.json").read_text(encoding="utf-8"))
    ok = [r for r in log if r.get("ok")]
    groups: dict[str, list] = defaultdict(list)
    for r in ok:
        exp = f"{r['category']}/{r['topic']}/{Path(r['rel']).stem}.md"
        groups[exp].append(r)
    losers = []
    for rs in groups.values():
        losers.extend(rs[:-1])  # 最后一个胜出，其余被覆盖
    return losers


def sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def convert_one_entry(rel: str, category: str, topic: str, dry_run: bool) -> dict:
    src = config.CORPUS_ROOT / rel
    out = {"rel": rel, "category": category, "topic": topic}
    if not src.exists():
        out.update(ok=False, err="源件不存在（已删/隔离）")
        return out
    doc_hash = sha16(src)
    out["doc_hash"] = doc_hash
    stem = convert_one.safe_name(Path(rel).stem)
    out_dir = MD_ROOT / CAT_MAP.get(category, category) / (convert_one.safe_name(topic) or "综合")
    md_path = out_dir / f"{stem}_{doc_hash[:8]}.md"
    out["md"] = str(md_path.relative_to(MD_ROOT).as_posix())
    if dry_run:
        out.update(ok=None, note="dry-run")
        return out
    try:
        ext = src.suffix.lower()
        if ext == ".pdf":
            md, pages, kept, skipped = convert_one.pdf_to_md(
                str(src), str(out_dir / "images"), stem + f"_{doc_hash[:8]}", "pymupdf4llm")
            ratio = convert_one.cjk_ratio(md)
            if ratio < 0.05:
                out.update(ok=False, err=f"低CJK({ratio:.1%})")
                return out
        elif ext == ".docx":
            md = convert_one.docx_to_md(str(src))
            pages, kept, skipped, ratio = 1, 0, 0, convert_one.cjk_ratio(md)
        elif ext == ".md":
            # md 源的"转换"就是拷贝（原转换器不支持 .md，这 2 条当时也是直接落 md）
            md = src.read_text(encoding="utf-8", errors="replace")
            pages, kept, skipped, ratio = 1, 0, 0, convert_one.cjk_ratio(md)
        else:
            out.update(ok=False, err=f"暂不支持 {ext}")
            return out
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        out.update(ok=True, chars=len(md), pages=pages, cjk=round(ratio, 3),
                   imgs_kept=kept, imgs_skipped=skipped)
    except Exception as ex:  # noqa: BLE001
        out.update(ok=False, err=f"{type(ex).__name__}: {str(ex)[:200]}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同名覆盖重转（输出名带 doc_hash 后缀）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--extra", nargs="*", default=[], help="追加补转的语料树相对路径")
    args = ap.parse_args(argv)

    jobs = losers_from_log()
    conn = db.connect(config.DB_PATH, create=False)
    # 追加补转件：category/topic 取 DB 行
    for rel in args.extra:
        posix = rel.replace("\\", "/")
        name = Path(posix).name
        row = conn.execute(
            "SELECT category, topic, dir_path FROM source_documents WHERE file_name=? AND status='classified'",
            (name,)).fetchone()
        if row:
            jobs.append({"rel": str(Path(row["dir_path"]).joinpath(name)), "category": row["category"],
                         "topic": row["topic"] or "综合", "_extra": True})
        else:
            print(f"[warn] extra 未找到 classified 行: {rel}", file=sys.stderr)
    conn.close()

    started = time.perf_counter()
    results = [convert_one_entry(j["rel"], j["category"], j["topic"], args.dry_run) for j in jobs]
    elapsed = time.perf_counter() - started
    if not args.dry_run:
        (BUILD / "reconvert_log.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok_n = sum(1 for r in results if r["ok"] is True)
    skip_n = sum(1 for r in results if r["ok"] is False and r.get("err") == "源件不存在（已删/隔离）")
    fail_n = sum(1 for r in results if r["ok"] is False and r.get("err") != "源件不存在（已删/隔离）")
    print(json.dumps({"jobs": len(results), "ok": ok_n, "skipped_src_gone": skip_n,
                      "failed": fail_n, "dry_run": args.dry_run,
                      "elapsed_sec": round(elapsed, 1),
                      "failures": [r for r in results if r["ok"] is False][:30]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
