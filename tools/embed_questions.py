# -*- coding: utf-8 -*-
"""P3-② embed_questions：bge-small-zh-v1.5 (fastembed ONNX) 对 questions 表编码 → 面经库.db 向量列。

- 模型：BAAI/bge-small-zh-v1.5（512 维，~100MB ONNX；首跑自动下载，默认走 hf-mirror 国内镜像）
- 编码对象：q_text（题干最能代表题目语义；答案噪声大不编码）
- 落库：vec_questions USING vec0(embedding float[512])，rowid=qid；DROP+CREATE 全量重建
用法：
    python tools/embed_questions.py            # 全量编码（首次含模型下载）
    python tools/embed_questions.py --check    # 只验证模型可用性
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
from kb import config  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
MODEL = "BAAI/bge-small-zh-v1.5"
DIM = 512
BATCH = 256
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # fastembed 走国内镜像

_MODEL = None


def get_model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(MODEL)
    return _MODEL


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_model()
    return [list(v) for v in model.embed(texts)]


def run(out_db: Path = OUT_DB, batch: int = BATCH) -> dict:
    import sqlite3
    import sqlite_vec
    con = sqlite3.connect(str(out_db))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("DROP TABLE IF EXISTS vec_questions")
    con.execute(f"CREATE VIRTUAL TABLE vec_questions USING vec0(embedding float[{DIM}])")
    total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    started = time.perf_counter()
    done = 0
    model = get_model()
    while done < total:
        rows = con.execute(
            "SELECT qid, q_text FROM questions ORDER BY qid LIMIT ? OFFSET ?",
            (batch, done)).fetchall()
        if not rows:
            break
        vecs = model.embed([r[1][:500] for r in rows])
        con.executemany(
            "INSERT INTO vec_questions(rowid, embedding) VALUES (?, ?)",
            [(r[0], sqlite_vec.serialize_float32(list(v))) for r, v in zip(rows, vecs)])
        done += len(rows)
        print(f"[embed] {done}/{total}", file=sys.stderr, flush=True)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
    elapsed = round(time.perf_counter() - started, 1)
    con.close()
    return {"model": MODEL, "dim": DIM, "vectors": n, "questions": total,
            "elapsed_sec": elapsed, "db": str(out_db)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="questions 向量化")
    ap.add_argument("--check", action="store_true", help="只验证模型加载")
    args = ap.parse_args(argv)
    if args.check:
        get_model()
        print(json.dumps({"ok": True, "model": MODEL}, ensure_ascii=False))
        return 0
    print(json.dumps(run(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
