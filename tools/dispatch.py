# -*- coding: utf-8 -*-
"""P2-② dispatch：门禁 fail/review 件的分流（R2 §7 S1.6）。

路由判据（按序命中即定）：
1. superseded        源行 status=superseded —— 已有替代（判重/转换/上游），记 note 里的替代去向
2. skip              源行 status=rejected（学习大纲/非内容），不值得 OCR
3. ocr_done          build/ocr_text/<rel>.txt 存在 —— OCR 已跑，文本待定稿合并（另见 ocr_pages 未回填的注记）
4. supersede_suggest 同系列旧版本（v1.0/v2.0-JavaGuide 这类）且上游已有替代 —— 建议退役，待用户确认
5. review            gate state=review（低CJK 灰区）—— 进 review_cli 队列
6. needs_ocr         其余 fail —— 无替代源，排 OCR

输出：build/dispatch_plan.csv + 控制台摘要。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config, db  # noqa: E402

BUILD = Path(config.BUILD_DIR)   # P1-3 修复（2026-11-17 审核）：路径统一走 config，KB_* 环境变量可覆盖
OCR_TEXT = BUILD / "ocr_text"
SUPERSEDE_PATTERNS = (re.compile(r"^v[12]\.0-JavaGuide"),)  # V1.1 已由上游替代，同系列旧版一并建议退役


def decide(row: dict, status: str | None, note: str, has_ocr: bool) -> str:
    if status == "superseded":
        return "superseded"
    if status == "rejected":
        return "skip"
    if has_ocr:
        return "ocr_done"
    if any(rx.search(Path(row["rel"]).name) for rx in SUPERSEDE_PATTERNS):
        return "supersede_suggest"
    if row["state"] == "review":
        return "review"
    return "needs_ocr"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="门禁 fail/review 件分流")
    ap.add_argument("--report", default=str(BUILD / "gate_report.json"))
    ap.add_argument("--out", default=str(BUILD / "dispatch_plan.csv"))
    args = ap.parse_args(argv)

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = [r for r in report["rows"] if r["state"] in ("fail", "review")]
    conn = db.connect(config.DB_PATH, create=False)

    plan = []
    for r in rows:
        rel_posix = r["rel"].replace("\\", "/")
        name = Path(rel_posix).name
        dir_path = str(Path(rel_posix).parent)
        # 被隔离/移动过的源行 dir_path 已改成 99 区路径，gate rel 是旧路径——
        # 按 file_name 取全部行，优先同 dir_path，其次 superseded > classified > rejected
        cands = conn.execute(
            "SELECT status, note, doc_hash, dir_path FROM source_documents WHERE file_name=?",
            (name,)).fetchall()
        pri = {"superseded": 1, "classified": 2, "rejected": 3}
        row = None
        for c in sorted(cands, key=lambda c: (c["dir_path"] != dir_path, pri.get(c["status"], 9))):
            row = c
            break
        status = row["status"] if row else None
        note = (row["note"] or "") if row else ""
        has_ocr = (OCR_TEXT / (rel_posix + ".txt")).exists()
        route = decide(r, status, note, has_ocr)
        plan.append({
            "rel": rel_posix, "state": r["state"], "reason": r.get("reason", ""),
            "route": route, "status": status or "?",
            "alt": note[:120], "ocr_txt": "Y" if has_ocr else "",
        })
    conn.close()

    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["rel", "state", "reason", "route", "status", "alt", "ocr_txt"])
        w.writeheader()
        w.writerows(plan)

    dist = Counter(p["route"] for p in plan)
    print(json.dumps({"fail_review": len(plan), "route_dist": dict(dist), "out": args.out},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
