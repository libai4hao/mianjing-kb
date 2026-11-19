# -*- coding: utf-8 -*-
"""把一次小规模打标结果固化成可管理的审核样本集。

产物（目录默认 `<build>/samples/<set_id>/`）：
- manifest.json   样本集元数据、冻结的 qid 清单、文件清单
- samples.csv    人工审核用（UTF-8 BOM，Excel 可直接打开）
- samples.jsonl  程序读取用（一行一个样本）
- qids.txt       冻结 qid，用于后续同一批样本重跑/对比
- metrics.json   基线分布（topic/qtype/difficulty/其他率等）

样本只读源库，不写回；审核列全部留空，供人工填写。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from mianjing_kb.llm_client import redact

REVIEW_COLUMNS = [
    "review_status", "final_topic", "final_qtype", "final_difficulty",
    "final_position", "final_quality", "final_tags", "final_canonical_q",
    "error_tags", "reviewer", "reviewed_at", "review_note",
]

CSV_COLUMNS = [
    "sample_id", "sample_set", "split", "qid", "q_norm_hash", "q_text", "a_text",
    "a_text_len", "model_topic", "model_qtype", "model_difficulty",
    "model_position", "model_quality", "model_tags", "model_canonical_q",
    "model_provider", "model_name", "labeled_at", "redacted", "tokens",
    "latency_ms", *REVIEW_COLUMNS,
]


def _schema_version(db: Path) -> str | None:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        try:
            return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            return None
    finally:
        con.close()


def _load_rows(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT s.qid, s.q_norm_hash, s.topic, s.qtype, s.difficulty, "
            "       s.position, s.quality, s.tags, s.canonical_q, s.provider, "
            "       s.model, s.redacted, s.latency_ms, s.tokens, s.labeled_at, "
            "       q.q_text, q.a_text "
            "FROM labels_staging s LEFT JOIN questions q ON q.qid=s.qid "
            "ORDER BY s.qid").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _metrics(items: list[dict]) -> dict:
    def dist(key):
        d: dict[str, int] = {}
        for it in items:
            v = it.get(key)
            k = "null" if v in (None, "") else str(v)
            d[k] = d.get(k, 0) + 1
        return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))

    n = len(items) or 1
    return {
        "count": len(items),
        "topic": dist("model_topic"),
        "qtype": dist("model_qtype"),
        "difficulty": dist("model_difficulty"),
        "other_topic_rate": round(
            sum(1 for it in items if it.get("model_topic") == "其他") / n, 4),
        "redacted_count": sum(1 for it in items if it.get("redacted")),
        "avg_tokens": round(sum(it.get("tokens") or 0 for it in items) / n, 1),
        "avg_latency_ms": round(
            sum(it.get("latency_ms") or 0 for it in items) / n, 1),
    }


def export_samples(db: str | os.PathLike, out_root: str | os.PathLike,
                   set_id: str = "", force: bool = False) -> dict:
    dbp = Path(db)
    if not dbp.exists():
        raise FileNotFoundError(f"源库不存在：{dbp}")
    rows = _load_rows(dbp)
    if not rows:
        raise RuntimeError(f"{dbp} 的 labels_staging 为空，先跑 100 题打标")
    set_id = set_id or f"pilot100-{datetime.now():%Y%m%d}"
    out = Path(out_root) / set_id
    if out.exists() and not force:
        raise FileExistsError(f"样本集已存在：{out}；换 set_id 或加 --force")
    out.mkdir(parents=True, exist_ok=True)

    items = []
    for i, r in enumerate(rows, 1):
        q_text, q_hit = redact(r.get("q_text") or "")
        a_text, a_hit = redact(r.get("a_text") or "")
        if a_hit or q_hit:
            r["redacted"] = 1
        item = {
            "sample_id": f"S{i:03d}",
            "sample_set": set_id,
            "split": "review",
            "qid": r["qid"],
            "q_norm_hash": r.get("q_norm_hash"),
            "q_text": q_text,
            "a_text": a_text,
            "a_text_len": len(a_text),
            "model_topic": r.get("topic"),
            "model_qtype": r.get("qtype"),
            "model_difficulty": r.get("difficulty"),
            "model_position": r.get("position"),
            "model_quality": r.get("quality"),
            "model_tags": r.get("tags"),
            "model_canonical_q": r.get("canonical_q"),
            "model_provider": r.get("provider"),
            "model_name": r.get("model"),
            "labeled_at": r.get("labeled_at"),
            "redacted": r.get("redacted") or 0,
            "tokens": r.get("tokens"),
            "latency_ms": r.get("latency_ms"),
            **{k: "" for k in REVIEW_COLUMNS},
        }
        item["review_status"] = "pending"
        items.append(item)

    with (out / "samples.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(items)
    with (out / "samples.jsonl").open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    qids = [int(it["qid"]) for it in items]
    (out / "qids.txt").write_text("\n".join(map(str, sorted(qids))) + "\n",
                                  encoding="utf-8")
    metrics = _metrics(items)
    (out / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")

    manifest = {
        "sample_set": set_id,
        "version": 1,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "purpose": "审核与优化回归样本；固定 qid，后续同集重跑对比",
        "source_db": str(dbp),
        "source_table": "labels_staging",
        "schema_version": _schema_version(dbp),
        "count": len(items),
        "split": "review",
        "qids": sorted(qids),
        "files": {
            "manifest": "manifest.json",
            "review_csv": "samples.csv",
            "items_jsonl": "samples.jsonl",
            "qids": "qids.txt",
            "metrics": "metrics.json",
        },
        "review_status_values": ["pending", "agree", "edit", "reject"],
        "error_tag_values": [
            "topic_wrong", "qtype_wrong", "difficulty_wrong", "fragment",
            "answer_missing", "duplicate", "other",
        ],
        "metrics": metrics,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "sample_set": set_id, "count": len(items),
            "path": str(out), "metrics": metrics}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出打标审核样本集")
    ap.add_argument("--db", required=True, help="源 kb.sqlite（只读）")
    ap.add_argument("--out", default="", help="输出根目录（默认 <db>/samples）")
    ap.add_argument("--set-id", default="", help="样本集 ID（默认 pilot100-YYYYMMDD）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在样本集")
    args = ap.parse_args(argv)
    out_root = Path(args.out) if args.out else Path(args.db).parent / "samples"
    try:
        res = export_samples(args.db, out_root, args.set_id, args.force)
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(ex).__name__}: {ex}"},
                         ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
