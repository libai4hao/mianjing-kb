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
import uuid
from datetime import datetime
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

STAGING_STATES = ("READY", "CLEANUP", "ENCODING", "VERIFY", "COMMIT", "DONE")


def _staging_marker(staging: Path) -> Path:
    """旁路库的同名运行标记：.<db>.vec-staging-<run_id>.run.json。"""
    return staging.with_name(staging.name[:-3] + ".run.json")


def _process_start(pid: int) -> float | None:
    """进程创建时间；没有 psutil 时返回 None（调用方按保守策略处理）。"""
    try:
        import psutil
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 —— psutil 缺失/进程已退出
        return None


def _pid_alive(pid: int) -> bool:
    """只探测进程是否存在；Windows 下绝不能用 os.kill(pid, 0)（会 TerminateProcess）。"""
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001 —— psutil 不可用时走平台探测
        pass
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _marker_is_live(data: dict) -> bool:
    """标记对应的进程是否仍可能是同一任务。

    PID 会被重用：只要拿不到创建时间，就保守认为“可能仍活着”，宁可中止也不删。
    """
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False
    marker_start = data.get("process_start")
    current_start = _process_start(pid)
    if marker_start is None or current_start is None:
        return True
    try:
        return abs(float(marker_start) - current_start) < 1.0
    except (TypeError, ValueError):
        return True


def _write_staging_marker(staging: Path, run_id: str, state: str) -> None:
    if state not in STAGING_STATES:
        raise ValueError(f"未知 staging 状态：{state}")
    data = {
        "run_id": run_id,
        "pid": os.getpid(),
        "process_start": _process_start(os.getpid()),
        "db": str(staging),
        "state": state,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    marker = _staging_marker(staging)
    tmp = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, marker)


def _mark_staging(staging: Path, state: str) -> None:
    marker = _staging_marker(staging)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["state"] = state
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, marker)


def _cleanup_stale_staging(out_db: Path) -> None:
    """D-30：清掉历史崩溃残留；发现仍活着的任务就中止新任务，不能误删。

    标准入口持有 online 写锁，所以正常不会出现第二个活任务；这里再按运行标记
    校验 PID + 进程创建时间，防止 PID 重用或绕过标准入口。
    """
    parent = Path(out_db).parent
    prefix = f".{Path(out_db).name}.vec-staging-"
    for st in sorted(parent.glob(prefix + "*.db")):
        marker = _staging_marker(st)
        if marker.exists():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as ex:
                raise RuntimeError(f"staging 标记不可读，拒绝猜删：{marker}") from ex
            if _marker_is_live(data):
                raise RuntimeError(
                    f"发现仍在运行的 embed staging 任务（pid={data.get('pid')}）：{st}")
        for p in (st, Path(str(st) + "-wal"), Path(str(st) + "-shm"), marker):
            try:
                p.unlink(missing_ok=True)
            except OSError as ex:
                raise RuntimeError(f"历史 staging 清理失败：{p}（{ex}）") from ex
    # 只有标记、没有主库的残留也清掉；活标记同样中止。
    for marker in sorted(parent.glob(prefix + "*.run.json")):
        st = marker.with_name(marker.name[:-len(".run.json")] + ".db")
        if st.exists():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if _marker_is_live(data):
            raise RuntimeError(
                f"发现仍在运行的 embed 任务标记（pid={data.get('pid')}）：{marker}")
        try:
            marker.unlink(missing_ok=True)
        except OSError as ex:
            raise RuntimeError(f"历史 staging 标记清理失败：{marker}（{ex}）") from ex


def _discard_staging(staging: Path) -> None:
    """本次任务结束后的自清理；失败只告警，不掩盖主流程结果。"""
    for p in (staging, Path(str(staging) + "-wal"), Path(str(staging) + "-shm"),
              _staging_marker(staging)):
        try:
            p.unlink(missing_ok=True)
        except OSError as ex:
            print(f"[warn] staging 清理失败：{p}（{ex}）", file=sys.stderr, flush=True)


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

    D-30：只保留本次 run_id 的 staging；启动时先清历史残留，清理失败直接中止。
    """
    import sqlite3
    import sqlite_vec
    out_db = Path(out_db)
    _cleanup_stale_staging(out_db)
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    staging = out_db.with_name(f".{out_db.name}.vec-staging-{run_id}.db")
    _write_staging_marker(staging, run_id, "READY")
    _mark_staging(staging, "CLEANUP")
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
        _discard_staging(staging)
        raise

    try:
        st = sqlite3.connect(str(staging))
        st.enable_load_extension(True)
        sqlite_vec.load(st)
        st.enable_load_extension(False)
    except Exception:
        con.close()
        _discard_staging(staging)
        raise
    _mark_staging(staging, "ENCODING")
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
        _mark_staging(staging, "VERIFY")
        built = st.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        if built != total:
            raise RuntimeError(f"旁路向量数不一致：{built} != {total}")
        if question_set_hash(con) != qhash:
            raise RuntimeError("编码期间 questions 题集发生变化，拒绝切换向量表")

        con.execute("ATTACH DATABASE ? AS staging", (str(staging),))
        _mark_staging(staging, "COMMIT")
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
        _mark_staging(staging, "DONE")
    except Exception:
        # 模型/编码/切换任何一步失败都关闭主连接；旧 vec_questions 未被 DROP。
        con.close()
        raise
    finally:
        st.close()
        _discard_staging(staging)

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
