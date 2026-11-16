# -*- coding: utf-8 -*-
"""gate：文本质量门禁（`ok=true` 不等于内容可用）。

判据（docs/01 §3.3，R1 修正版）：
    CJK 占比 ≥ 0.15、乱码率 < 0.01、有效字符率 ≥ 0.90、单篇可见字符 ≥ 800
先判乱码 → 再判 CJK → 再判内容量，因为字体编码损坏会同时拖垮三个指标。

用法：
    python tools/gate.py --md-dir "<CORPUS_ROOT>\\20-题库MD" --top 20
    python tools/gate.py --md-dir ... --out build\\gate_report.json --no-db
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db, textmetrics  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="ignore")


def gate_file(path: Path) -> dict:
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
    ap = argparse.ArgumentParser(description="md 产物文本质量门禁")
    ap.add_argument("--md-dir", default=str(config.CORPUS_ROOT / "20-题库MD"))
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--no-db", action="store_true", help="不写库，只出报告")
    ap.add_argument("--out", default=None, help="报告 JSON 落盘路径")
    ap.add_argument("--top", type=int, default=15, help="打印最差的 N 个")
    ap.add_argument("--recursive", action="store_true", default=True)
    args = ap.parse_args(argv)

    md_dir = Path(args.md_dir)
    if not md_dir.exists():
        print(f"[fatal] 目录不存在: {md_dir}", file=sys.stderr)
        return 2

    files = sorted(md_dir.rglob("*.md"))
    results = [gate_file(p) for p in files]
    if not results:
        print("[info] 没有 md 文件")
        return 0

    by_verdict = Counter(r["verdict"] for r in results)
    summary = {
        "md_dir": str(md_dir),
        "files": len(results),
        "verdict": dict(by_verdict),
        "thresholds": {
            "cjk_min": config.GATE_CJK_MIN,
            "garbled_max": config.GATE_GARBLED_MAX,
            "valid_min": config.GATE_VALID_MIN,
            "min_visible": config.GATE_MIN_VISIBLE,
        },
    }

    bad = [r for r in results if r["verdict"] != "pass"]
    summary["fail_pages_chars"] = {
        "files": len(bad),
        "visible_chars": sum(r["visible_chars"] for r in bad),
    }

    if not args.no_db:
        conn = db.connect(args.db)
        for r in results:
            conn.execute(
                "INSERT OR REPLACE INTO gate_results "
                "(md_path, doc_hash, visible_chars, cjk_chars, cjk_ratio, garbled_ratio, valid_ratio, verdict, checked_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (r["md_path"], None, r["visible_chars"], r["cjk_chars"], r["cjk_ratio"],
                 r["garbled_ratio"], r["valid_ratio"], r["verdict"], datetime.now().strftime(NOW_FMT)),
            )
        conn.commit()
        conn.close()

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if bad:
        print(f"\n--- 最差 {min(args.top, len(bad))} 个 ---")
        bad.sort(key=lambda r: (r["cjk_ratio"], -r["visible_chars"]))
        for r in bad[: args.top]:
            name = Path(r["md_path"]).name
            print(f"{r['verdict']:<9} CJK={r['cjk_ratio']:.3f} 乱码={r['garbled_ratio']:.3f} "
                  f"有效={r['valid_ratio']:.3f} 字数={r['visible_chars']:>7} | {name[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
