# -*- coding: utf-8 -*-
"""refresh：检索库影子重建 + G1-G5 门禁 + 原子切换（docs/15 §2.1 / K1，2026-11-17 落地）。

流程：
  1. 影子重建：build_db.build(out_db=BUILD/面经库_shadow.db)——复用现有 build_db 全量重建
  2. 向量回填：从现行 面经库.db 拷 vec_questions（sqlite_vec 可用时）；现行库无向量表 → 跳过；
     回填失败 → 中止（绝不换出会丢向量的检索库）。新题入库后用 --reembed 重跑向量化替代拷贝。
  3. 门禁：verify_ledger G1-G5 对影子库实跑（台账=现行 kb.sqlite，检索库=影子）
  4. 硬门禁 FAIL → 删影子、线上不动、exit 1（门禁的阻止力，不是"报个警就继续"）
     PASS → os.replace(影子, 线上) 同卷原子切换，并清理旧库 WAL 侧车

与 docs/15 原文的偏差（诚实声明）：docs/15 以 kb_shadow.sqlite 为例，本实现把影子保护
对象放在重建风险最高的 面经库.db——build_db 是 DROP+CREATE 原地重建，中断即损坏；
kb.sqlite 的批量写入本身走单事务（intake/classify 末尾一次 commit），无需影子。

WAL 侧车注意（Windows 实测坑）：os.replace 只换主文件，旧库的 -wal/-shm 必须一并清理，
否则 sqlite 会把旧 WAL 硬套在新主文件上。切换前也先清影子的残留侧车。

用法：
    python tools/refresh.py               # 影子重建 → 向量拷贝 → G1-G5 → 通过才切换
    python tools/refresh.py --reembed     # 影子重建后重跑向量化（新题入库后必用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config  # noqa: E402

SHADOW_NAME = "面经库_shadow.db"
ONLINE_NAME = "面经库.db"


def _sidecars(p: Path) -> tuple[Path, Path]:
    return Path(str(p) + "-wal"), Path(str(p) + "-shm")


def _cleanup(shadow: Path) -> list[str]:
    """删影子及其侧车；连接未释放等导致的失败不掩盖主因，逐项记因返回。"""
    notes = []
    for p in (shadow, *_sidecars(shadow)):
        try:
            p.unlink(missing_ok=True)
        except OSError as ex:
            notes.append(f"{p.name}: {type(ex).__name__}")
    return notes


def build_shadow(shadow: Path) -> dict:
    from build_db import build
    return build(out_db=shadow)


def copy_vec(src_db: Path, dst_db: Path) -> str:
    """把现行库的 vec_questions 回填进影子库。返回 copied N / absent / error:..."""
    import sqlite3
    con = sqlite3.connect(str(dst_db))
    try:
        con.execute("ATTACH DATABASE ? AS cur", (str(src_db),))
        has = con.execute("SELECT COUNT(*) FROM cur.sqlite_master "
                          "WHERE type='table' AND name='vec_questions'").fetchone()[0]
        if not has:
            return "absent（现行库无向量表）"
        try:
            import sqlite_vec
        except ImportError:
            return "error: sqlite_vec 不可用，无法回填向量（拒绝换出丢向量的库）"
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        from embed_questions import DIM
        con.execute(f"CREATE VIRTUAL TABLE vec_questions USING vec0(embedding float[{DIM}])")
        con.execute("INSERT INTO vec_questions(rowid, embedding) "
                    "SELECT rowid, embedding FROM cur.vec_questions")
        n_dst = con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        n_src = con.execute("SELECT COUNT(*) FROM cur.vec_questions").fetchone()[0]
        if n_dst != n_src:
            return f"error: 向量回填行数不一致 {n_dst}!={n_src}"
        con.commit()
        return f"copied {n_dst}"
    except Exception as ex:  # noqa: BLE001
        return f"error: {type(ex).__name__}: {str(ex)[:120]}"
    finally:
        con.close()


def run_gates(shadow: Path) -> list[dict]:
    """G1-G5 对影子库实跑（verify_ledger 同款口径；G6 打标未启用、G7 属 pre-commit 不在此跑）。"""
    import sqlite3
    from verify_ledger import (g1_coverage, g2_chain, g3_topic, g4_index, g5_disk,
                               load_skip_log)
    kb_conn = sqlite3.connect(f"file:{Path(config.DB_PATH).as_posix()}?mode=ro", uri=True)
    kb_conn.row_factory = sqlite3.Row
    out_conn = sqlite3.connect(f"file:{Path(shadow).as_posix()}?mode=ro", uri=True)
    out_conn.row_factory = sqlite3.Row
    try:
        import sqlite_vec
        out_conn.enable_load_extension(True)
        sqlite_vec.load(out_conn)
        out_conn.enable_load_extension(False)
    except ImportError:
        pass
    skip = load_skip_log()
    gates = [
        g1_coverage(kb_conn, out_conn, skip),
        g2_chain(out_conn, Path(config.QUESTION_MD_DIR), Path(config.BUILD_DIR) / "ocr_text", skip),
        g3_topic(out_conn),
        g4_index(out_conn),
        g5_disk(kb_conn, Path(config.CORPUS_ROOT), Path(config.BUILD_DIR)),
    ]
    kb_conn.close()
    out_conn.close()
    return gates


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检索库影子重建 + G1-G5 门禁 + 原子切换")
    ap.add_argument("--reembed", action="store_true",
                    help="影子重建后重跑向量化（新题入库后必用；默认只拷现行库向量）")
    args = ap.parse_args(argv)

    started = time.perf_counter()
    build_dir = Path(config.BUILD_DIR)
    shadow = build_dir / SHADOW_NAME
    online = build_dir / ONLINE_NAME

    try:
        _cleanup(shadow)                       # 上次中断残留；WAL 侧车绝不跨库复用

        stats = build_shadow(shadow)
        vec = "absent（线上库不存在）" if not online.exists() else copy_vec(online, shadow)
        if args.reembed:
            from embed_questions import run as embed_run
            emb = embed_run(out_db=shadow)
            vec = f"reembed {emb.get('vectors')} vectors"
        if vec.startswith("error"):
            _cleanup(shadow)
            print(json.dumps({"verdict": "ABORT", "why": f"向量回填失败: {vec}",
                              "note": "线上 面经库.db 未动；可 --reembed 重跑向量化后重试"},
                             ensure_ascii=False, indent=1))
            return 1

        gates = run_gates(shadow)
        hard_fail = [g["id"] for g in gates if g["level"] == "硬" and g["state"] == "FAIL"]
        summary = {
            "verdict": "PASS" if not hard_fail else "FAIL",
            "build": {k: stats[k] for k in ("documents", "questions", "doc_contents", "size_mb")},
            "vec": vec,
            "gates": {g["id"]: g["state"] for g in gates},
            "hard_fail": hard_fail,
            "elapsed_sec": round(time.perf_counter() - started, 1),
        }
        if hard_fail:
            _cleanup(shadow)
            summary["note"] = "影子库已删除，线上 面经库.db 未动（门禁阻止力，docs/15 §2.1）"
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return 1

        # 原子切换（同卷 os.replace）+ 旧库 WAL 侧车清理：侧车属于被废弃的旧库，
        # 留着会被 sqlite 硬套到新主文件上（Windows 实测坑，见模块 docstring）
        os.replace(shadow, online)
        for p in _sidecars(online):
            p.unlink(missing_ok=True)
        summary["note"] = "影子库已原子切换为线上 面经库.db"
        print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        return 0
    except Exception as ex:  # noqa: BLE001 —— 刷新失败必须保住线上库
        leftover = _cleanup(shadow)
        print(json.dumps({"verdict": "ABORT", "why": f"{type(ex).__name__}: {str(ex)[:200]}",
                          "leftover_locked": leftover,
                          "note": "影子库已删除，线上 面经库.db 未动"},
                         ensure_ascii=False, indent=1))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
