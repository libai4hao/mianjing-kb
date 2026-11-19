# -*- coding: utf-8 -*-
"""P3-② embed_questions：bge-small-zh-v1.5 (fastembed ONNX) 对 questions 表编码 → 面经库.db 向量列。

- 模型：BAAI/bge-small-zh-v1.5（512 维，~100MB ONNX；首跑自动下载，默认走 hf-mirror 国内镜像）
- 编码对象：q_text（题干最能代表题目语义；答案噪声大不编码）
- 落库：vec_questions USING vec0(embedding float[512])，rowid=qid；DROP+CREATE 全量重建
用法：
    python -m mianjing_kb.embed_questions            # 全量编码（首次含模型下载）
    python -m mianjing_kb.embed_questions --check    # 只验证模型可用性
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
sys.path.insert(0, str(REPO / "mianjing_kb"))
from mianjing_kb.kb import config, wlock  # noqa: E402

OUT_DB = Path(config.BUILD_DIR) / "面经库.db"
MODEL = "BAAI/bge-small-zh-v1.5"
DIM = 512
BATCH = 256
# T1/T22：可下推的过滤字段进 vec0 metadata；position/quality/tags 是多选/自由文本，
# 不能做等值 metadata，留在 questions/question_tags 侧做过采样后的 SQL 过滤。
VEC_META_COLUMNS = ("topic", "qtype", "difficulty", "q_key")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # fastembed 走国内镜像

_MODEL = None


def vec_schema() -> str:
    """vec0 建表 DDL 的唯一来源（embed / refresh.copy_vec / 测试共用）。"""
    return (f"embedding float[{DIM}], "
            "topic text, qtype text, difficulty integer, q_key text")


def create_vec_table(con) -> None:
    con.execute(f"CREATE VIRTUAL TABLE vec_questions USING vec0({vec_schema()})")


def vec_columns(con, name: str = "vec_questions", schema: str = "main") -> set[str]:
    """vec0 的列名；name 不存在返回空集。schema 可为 cur/main（ATTACH 后校验源表）。"""
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM pragma_table_info(?, ?)", (name, schema))}
    except Exception:  # noqa: BLE001 —— 表不存在/扩展未加载
        return set()


def vec_metadata_ready(con, name: str = "vec_questions", schema: str = "main") -> bool:
    return set(VEC_META_COLUMNS) <= vec_columns(con, name, schema)


def question_set_hash(con) -> str:
    """题集指纹：按稳定键排序后 sha256。copy_vec 用它拒绝"向量旧、题集新"的错配。"""
    import hashlib
    rows = [str(r[0] or "") for r in con.execute(
        "SELECT COALESCE(NULLIF(q_key,''), doc_hash || ':' || q_norm_hash) AS k "
        "FROM questions ORDER BY k")]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def write_vec_meta(con, qhash: str | None = None) -> None:
    """记录向量表身份：模型 / 维数 / 题集指纹 / 构建时间（普通表，不属于 vec0）。"""
    from datetime import datetime
    con.execute("CREATE TABLE IF NOT EXISTS vec_meta("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    vals = {"model": MODEL, "dim": str(DIM),
            "question_set_hash": qhash or question_set_hash(con),
            "built_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    con.executemany("INSERT INTO vec_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(vals.items()))


def read_vec_meta(con) -> dict[str, str]:
    try:
        return {r[0]: r[1] for r in con.execute("SELECT key,value FROM vec_meta")}
    except Exception:  # noqa: BLE001
        return {}


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
    """重建向量：先在旁路库编码并校验，再事务内替换线上 vec_questions。

    顺序很关键：模型加载 → 旁路编码 → 数量/题集指纹校验 → 事务切换 → 写 vec_meta。
    任何编码/模型失败都发生在替换之前，线上旧向量表保持可用。
    """
    import sqlite3
    import sqlite_vec
    out_db = Path(out_db)
    staging = out_db.with_name(f".{out_db.name}.vec-staging-{os.getpid()}.db")
    if staging.exists():
        staging.unlink()
    con = sqlite3.connect(str(out_db))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    qhash = question_set_hash(con)
    started = time.perf_counter()

    # 先初始化模型，绝不能先 DROP 旧表；模型下载/ONNX 初始化失败时旧索引仍在。
    try:
        model = get_model()
    except Exception:
        con.close()
        raise

    st = sqlite3.connect(str(staging))
    st.enable_load_extension(True)
    sqlite_vec.load(st)
    st.enable_load_extension(False)
    try:
        st.execute(f"CREATE VIRTUAL TABLE vec_questions USING vec0({vec_schema()})")
        done = 0
        while True:
            rows = con.execute(
                "SELECT qid, q_text, topic, qtype, difficulty, q_key FROM questions "
                "ORDER BY qid LIMIT ? OFFSET ?",
                (batch, done)).fetchall()
            if not rows:
                break
            vecs = model.embed([r[1][:500] for r in rows])
            st.executemany(
                "INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty, q_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(r[0], sqlite_vec.serialize_float32(list(v)),
                  r[2] or "", r[3] or "", r[4] if r[4] is not None else -1,
                  r[5] or "") for r, v in zip(rows, vecs)])
            done += len(rows)
            print(f"[embed] {done}/{total}", file=sys.stderr, flush=True)
        st.commit()
        built = st.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        if built != total:
            raise RuntimeError(f"旁路向量数不一致：{built} != {total}")
        if question_set_hash(con) != qhash:
            raise RuntimeError("编码期间 questions 题集发生变化，拒绝切换向量表")

        con.execute("ATTACH DATABASE ? AS staging", (str(staging),))
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DROP TABLE IF EXISTS main.vec_questions")
            create_vec_table(con)
            con.execute(
                "INSERT INTO main.vec_questions(rowid, embedding, topic, qtype, difficulty, q_key) "
                "SELECT rowid, embedding, topic, qtype, difficulty, q_key FROM staging.vec_questions")
            write_vec_meta(con, qhash)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("DETACH DATABASE staging")
    except Exception:
        # 模型/编码/切换任何一步失败都关闭主连接；旧 vec_questions 未被 DROP。
        con.close()
        raise
    finally:
        st.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(staging) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    n = con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
    elapsed = round(time.perf_counter() - started, 1)
    con.close()
    return {"model": MODEL, "dim": DIM, "vectors": n, "questions": total,
            "question_set_hash": qhash,
            "elapsed_sec": elapsed, "db": str(out_db)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="questions 向量化")
    ap.add_argument("--check", action="store_true", help="只验证模型加载")
    args = ap.parse_args(argv)
    if args.check:
        get_model()
        print(json.dumps({"ok": True, "model": MODEL}, ensure_ascii=False))
        return 0
    online = Path(config.BUILD_DIR) / "面经库.db"
    try:
        with wlock.write_lock("online", tool="embed", build_dir=online.parent, target=online):
            print(json.dumps(run(), ensure_ascii=False, indent=1))
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另有工具在写线上库（refresh/build_db/embed）；稍后重试"},
                         ensure_ascii=False, indent=1))
        return 2
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
