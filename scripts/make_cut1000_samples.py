# -*- coding: utf-8 -*-
"""生成切题/原始对照样本：1000 道去重切题样本 + 100 篇原始 Markdown + 逐题原文片段。

只读 kb.sqlite 与题库 Markdown，不写生产库。默认输出到
`<build>/samples/cut1000-raw100-<YYYYMMDD>/`。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath

CSV_COLUMNS = [
    "sample_id", "qid", "q_key", "q_norm_hash", "doc_hash", "md_rel", "file_name",
    "category", "doc_topic", "effective_topic", "q_text", "a_text", "span_start",
    "span_end", "source_text_len", "source_text_match", "qtype", "difficulty", "tags",
    "quality", "position", "canonical_q", "label_kind", "label_topic", "label_qtype",
    "label_difficulty", "label_tags", "label_quality", "label_position",
    "label_canonical_q", "label_source", "label_model", "labeled_at", "pii_hit",
    "answer_missing",
]

DANGEROUS = ("=", "+", "-", "@")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _ro_uri(path: Path) -> str:
    """只读 URI：路径必须编码，`#`/`?` 不编码会被当成片段/查询串改掉目标库。"""
    return Path(path).resolve().as_uri() + "?mode=ro"


def csv_safe(v) -> str:
    """公式中和（与 kb/csvsafe 同口径）：前导空白后的公式字符也算。"""
    s = "" if v is None else str(v)
    if s.startswith(("\t", "\r", "\n")) or s.lstrip(" \t\r\n").startswith(DANGEROUS):
        return "'" + s
    return s


def _load_rows(db: Path) -> list[dict]:
    con = sqlite3.connect(_ro_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT q.qid, q.q_key, q.q_norm_hash, q.doc_hash, q.seq, q.q_text, q.a_text, "
            "       q.span_start, q.span_end, q.qtype, q.difficulty, q.tags, q.quality, "
            "       q.position, q.canonical_q, q.topic AS q_topic, "
            "       dp.md_rel, sd.file_name, sd.category, sd.topic AS doc_topic, "
            "       lf.topic AS fact_topic, lf.qtype AS fact_qtype, "
            "       lf.difficulty AS fact_difficulty, lf.tags AS fact_tags, "
            "       lf.labeled_at AS fact_labeled_at, lf.source AS fact_source "
            "FROM questions q "
            "JOIN document_products dp ON dp.doc_hash=q.doc_hash "
            "LEFT JOIN source_documents sd ON sd.doc_hash=q.doc_hash "
            "LEFT JOIN label_facts lf ON lf.q_norm_hash=q.q_norm_hash "
            "WHERE dp.md_rel IS NOT NULL AND LENGTH(TRIM(q.q_text)) >= 8 "
            "ORDER BY q.q_norm_hash, LENGTH(COALESCE(q.a_text,'')) DESC, q.qid").fetchall()]
        return rows
    finally:
        con.close()


def _staging_labels(db: Path) -> dict[str, dict]:
    con = sqlite3.connect(_ro_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {r["q_norm_hash"]: dict(r) for r in con.execute(
            "SELECT q_norm_hash, topic, qtype, difficulty, tags, position, quality, "
            "       canonical_q, provider, model, labeled_at FROM labels_staging "
            "WHERE q_norm_hash IS NOT NULL")}
    finally:
        con.close()


def _schema_version(db: Path) -> str | None:
    con = sqlite3.connect(_ro_uri(db), uri=True)
    try:
        try:
            return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            return None
    finally:
        con.close()


def _safe_stem(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", s).strip("._")
    return (s[:max_len] or "doc")


def _md_path(root: Path, md_rel: str) -> Path:
    parts = PurePosixPath(md_rel.replace("\\", "/")).parts
    if not parts or any(p in ("", "..") for p in parts):
        raise ValueError(f"非法 md_rel：{md_rel}")
    p = root.joinpath(*parts)
    rp = p.resolve()
    if root.resolve() not in rp.parents:
        raise ValueError(f"md_rel 越界：{md_rel}")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--md-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default="", help="mianjing-kb 仓库根；用于导入 text_of")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--raw-docs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20261120)
    args = ap.parse_args(argv)

    repo = Path(args.repo) if args.repo else Path.cwd()
    sys.path.insert(0, str(repo))
    from mianjing_kb.extract import text_of  # noqa: E402
    from mianjing_kb.llm_client import redact  # noqa: E402

    db = Path(args.db)
    root = Path(args.md_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(db)
    staging = _staging_labels(db)

    # 每个 q_norm_hash 取答案最长的一条，避免同题多文档重复占样本。
    by_hash: dict[str, dict] = {}
    for r in rows:
        by_hash.setdefault(r["q_norm_hash"], r)
    hashes = sorted(by_hash)
    if len(hashes) < args.n:
        raise RuntimeError(f"可抽样去重题不足：{len(hashes)} < {args.n}")
    rng = random.Random(args.seed)
    selected_hashes = rng.sample(hashes, args.n)
    selected = [by_hash[h] for h in selected_hashes]
    selected.sort(key=lambda r: (r["doc_hash"], r["seq"], r["qid"]))
    for i, r in enumerate(selected, 1):
        r["sample_id"] = f"S{i:04d}"

    text_cache: dict[str, str] = {}
    items = []
    spans = []
    pii_count = 0
    for r in selected:
        md_path = _md_path(root, r["md_rel"])
        if r["md_rel"] not in text_cache:
            text_cache[r["md_rel"]] = text_of(md_path) or ""
        text = text_cache[r["md_rel"]]
        st, en = int(r["span_start"]), int(r["span_end"])
        source_text = text[st:en] if 0 <= st <= en <= len(text) else ""
        context_before = text[max(0, st - 300):st]
        context_after = text[en:en + 300]
        q_hit = bool(redact(r["q_text"] or "")[1])
        a_hit = bool(redact(r["a_text"] or "")[1])
        if q_hit or a_hit:
            pii_count += 1

        lab = staging.get(r["q_norm_hash"])
        if r["fact_topic"] is not None:
            label_kind = "merged"
            label = {
                "topic": r["fact_topic"], "qtype": r["fact_qtype"],
                "difficulty": r["fact_difficulty"], "tags": r["fact_tags"],
                "quality": None, "position": None, "canonical_q": None,
                "source": r["fact_source"], "model": None,
                "labeled_at": r["fact_labeled_at"],
            }
        elif lab:
            label_kind = "staging"
            label = lab
        else:
            label_kind = ""
            label = {}

        item = {
            "sample_id": r["sample_id"], "qid": r["qid"], "q_key": r["q_key"],
            "q_norm_hash": r["q_norm_hash"], "doc_hash": r["doc_hash"],
            "md_rel": r["md_rel"], "file_name": r["file_name"],
            "category": r["category"], "doc_topic": r["doc_topic"],
            "effective_topic": r["q_topic"] or r["doc_topic"] or "",
            "q_text": r["q_text"], "a_text": r["a_text"],
            "span_start": st, "span_end": en, "source_text_len": len(source_text),
            "source_text_match": 1 if source_text else 0,
            "qtype": r["qtype"], "difficulty": r["difficulty"], "tags": r["tags"],
            "quality": r["quality"], "position": r["position"],
            "canonical_q": r["canonical_q"], "label_kind": label_kind,
            "label_topic": label.get("topic"), "label_qtype": label.get("qtype"),
            "label_difficulty": label.get("difficulty"),
            "label_tags": label.get("tags"), "label_quality": label.get("quality"),
            "label_position": label.get("position"),
            "label_canonical_q": label.get("canonical_q"),
            "label_source": label.get("source") or label.get("provider"),
            "label_model": label.get("model"), "labeled_at": label.get("labeled_at"),
            "pii_hit": 1 if (q_hit or a_hit) else 0,
            "answer_missing": 0 if (r["a_text"] or "").strip() else 1,
        }
        items.append(item)
        spans.append({
            "sample_id": r["sample_id"], "qid": r["qid"], "q_key": r["q_key"],
            "q_norm_hash": r["q_norm_hash"], "doc_hash": r["doc_hash"],
            "md_rel": r["md_rel"], "span_start": st, "span_end": en,
            "source_text": source_text, "context_before": context_before,
            "context_after": context_after,
            "q_text": r["q_text"], "a_text": r["a_text"],
            "qtype": r["qtype"], "effective_topic": item["effective_topic"],
        })

    with (out / "cut_samples_1000.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for it in items:
            w.writerow({k: csv_safe(it.get(k)) for k in CSV_COLUMNS})
    with (out / "cut_samples_1000.jsonl").open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    with (out / "raw_spans_1000.jsonl").open("w", encoding="utf-8") as fh:
        for it in spans:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    # 原始 Markdown 样本：从被抽到题的文档中随机取 100 篇，整篇保留。
    docs: dict[str, dict] = {}
    sampled_per_doc = Counter(it["doc_hash"] for it in items)
    for r in rows:
        if r["doc_hash"] not in docs:
            docs[r["doc_hash"]] = {
                "doc_hash": r["doc_hash"], "md_rel": r["md_rel"],
                "file_name": r["file_name"], "category": r["category"],
                "doc_topic": r["doc_topic"],
            }
    doc_keys = sorted(d for d in docs if sampled_per_doc.get(d, 0) > 0)
    raw_n = min(args.raw_docs, len(doc_keys))
    raw_doc_keys = rng.sample(doc_keys, raw_n)
    raw_doc_keys.sort(key=lambda d: docs[d]["md_rel"])
    raw_dir = out / "raw_docs"
    raw_dir.mkdir(exist_ok=True)
    raw_manifest = []
    for i, dh in enumerate(raw_doc_keys, 1):
        meta = docs[dh]
        src = _md_path(root, meta["md_rel"])
        dst = raw_dir / f"{i:03d}__{_safe_stem(src.stem)}.md"
        shutil.copyfile(src, dst)
        b = dst.read_bytes()
        raw_manifest.append({
            **meta, "copied_as": f"raw_docs/{dst.name}",
            "bytes": len(b), "sha256": sha256_bytes(b),
            "questions_total": sum(1 for r in rows if r["doc_hash"] == dh),
            "sampled_questions": sampled_per_doc.get(dh, 0),
        })

    with (out / "raw_docs_manifest.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = ["doc_hash", "md_rel", "file_name", "category", "doc_topic",
                  "copied_as", "bytes", "sha256", "questions_total", "sampled_questions"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(raw_manifest)

    def dist(key):
        c = Counter(str(it.get(key) if it.get(key) not in (None, "") else "null")
                    for it in items)
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    metrics = {
        "count": len(items),
        "distinct_q_norm_hash": len({it["q_norm_hash"] for it in items}),
        "distinct_docs": len({it["doc_hash"] for it in items}),
        "effective_topic": dist("effective_topic"),
        "qtype": dist("qtype"),
        "difficulty": dist("difficulty"),
        "label_kind": dist("label_kind"),
        "label_coverage": round(sum(1 for it in items if it["label_kind"]) / len(items), 4),
        "answer_missing": sum(it["answer_missing"] for it in items),
        "pii_hit": pii_count,
        "source_text_match": sum(it["source_text_match"] for it in items),
        "raw_docs": raw_n,
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")

    manifest = {
        "sample_set": out.name,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": args.seed,
        "count": len(items),
        "raw_docs": raw_n,
        "source_db": str(db),
        "md_root": str(root),
        "schema_version": _schema_version(db),
        "inclusion": "md-backed questions; q_text >= 8; one longest-answer row per q_norm_hash; random.sample(seed)",
        "files": {
            "cut_csv": "cut_samples_1000.csv",
            "cut_jsonl": "cut_samples_1000.jsonl",
            "raw_spans": "raw_spans_1000.jsonl",
            "raw_docs_manifest": "raw_docs_manifest.csv",
            "raw_docs_dir": "raw_docs/",
            "metrics": "metrics.json",
            "readme": "README.md",
            "verify": "verify_samples.py",
        },
        "metrics": metrics,
        "note": "内部测试样本；含原始题目文本，可能有联系方式，不要提交到公开仓库或上传外部服务。",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "count": len(items),
                      "raw_docs": raw_n, "metrics": metrics}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
