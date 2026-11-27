# -*- coding: utf-8 -*-
"""生成"测试库"：从生产库只读抽样 N 题，写入独立 SQLite（默认 <build>/kb_test.sqlite）。

设计约束：
  - 生产库只用 `mode=ro` 打开，绝不写；
  - 测试库已存在则拒绝（除非 --force），防误删测试数据；
  - `--force` 也先只读校验源库、抽样成功后才原子替换目标：源库损坏/为空时旧测试库保持原样；
  - 复制 questions / doc_contents / source_documents 的共有列，保证离线打标与检索演示可用。

用法：
    python scripts/seed_test_db.py --questions 200
    python scripts/seed_test_db.py --dst build/kb_test.sqlite --questions 100 --force
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from mianjing_kb.kb import config, db  # noqa: E402


def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _has_table(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _copy_rows(src, dst, table: str, qids: list[int]) -> int:
    """按 qid 取样复制 questions，并连带复制相关 docs（只复制两边都有的列）。"""
    if not (_has_table(src, table) and _has_table(dst, table)):
        return 0
    common = [c for c in _cols(src, table) if c in _cols(dst, table)]
    if table == "questions":
        rows = src.execute(
            f"SELECT {','.join(common)} FROM questions WHERE qid IN ({','.join(map(str, qids))})"
        ).fetchall()
        dst.executemany(f"INSERT OR REPLACE INTO questions ({','.join(common)}) "
                        f"VALUES ({','.join('?' * len(common))})", rows)
        return len(rows)
    if table == "doc_contents":
        hashes = {r[0] for r in src.execute(
            "SELECT doc_hash FROM questions WHERE qid IN (%s)" % ",".join(map(str, qids)))}
        hs = ",".join("?" * len(hashes))
        rows = src.execute(f"SELECT {','.join(common)} FROM doc_contents "
                           f"WHERE doc_hash IN ({hs})", tuple(hashes)).fetchall()
        dst.executemany(f"INSERT OR REPLACE INTO doc_contents ({','.join(common)}) "
                        f"VALUES ({','.join('?' * len(common))})", rows)
        return len(rows)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从生产库抽样生成测试库")
    ap.add_argument("--src", default=str(config.DB_PATH), help="生产库（只读打开）")
    ap.add_argument("--dst", default=str(Path(config.BUILD_DIR) / "kb_test.sqlite"))
    ap.add_argument("--questions", type=int, default=200, help="抽样题数")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    args = ap.parse_args(argv)

    src_path = Path(args.src).resolve()
    dst_path = Path(args.dst).resolve()
    if not src_path.exists():
        print(json.dumps({"fatal": f"源库不存在: {src_path}"}, ensure_ascii=False))
        return 2
    if dst_path == src_path:
        print(json.dumps({"fatal": f"目标与源库同一文件，拒绝覆盖: {dst_path}"}, ensure_ascii=False))
        return 2
    if dst_path.exists() and not args.force:
        print(json.dumps({"fatal": f"目标已存在（--force 覆盖）: {dst_path}"}, ensure_ascii=False))
        return 2

    # 先只读校验源库并定下样本，全部通过后才动目标：源库异常时旧测试库必须原样保留。
    src = sqlite3.connect(db.ro_uri(src_path), uri=True)
    src.row_factory = sqlite3.Row
    try:
        if not _has_table(src, "questions"):
            print(json.dumps({"fatal": "源库缺少 questions 表"}, ensure_ascii=False))
            return 2
        all_qids = [r[0] for r in src.execute("SELECT qid FROM questions ORDER BY qid")]
        if not all_qids:
            print(json.dumps({"fatal": "源库 questions 为空"}, ensure_ascii=False))
            return 2
        qids = sorted(random.Random(args.seed).sample(all_qids, min(args.questions, len(all_qids))))

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dst_path.with_name(f"{dst_path.name}.tmp-{os.getpid()}")
        tmp_path.unlink(missing_ok=True)
        try:
            dst = db.connect(tmp_path)        # 建全量 schema（含 013 标签配置表 seed）
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        try:
            counts = {"questions": _copy_rows(src, dst, "questions", qids),
                      "doc_contents": _copy_rows(src, dst, "doc_contents", qids)}
            hashes = {r[0] for r in src.execute(
                "SELECT doc_hash FROM questions WHERE qid IN (%s)" % ",".join(map(str, qids)))}
            if _has_table(src, "source_documents") and _has_table(dst, "source_documents") and hashes:
                src_cols = _cols(src, "source_documents")
                dst_cols = _cols(dst, "source_documents")
                common = [c for c in src_cols if c in dst_cols]
                hs = ",".join("?" * len(hashes))
                rows = src.execute(f"SELECT {','.join(common)} FROM source_documents "
                                   f"WHERE doc_hash IN ({hs})", tuple(hashes)).fetchall()
                dst.executemany(f"INSERT OR REPLACE INTO source_documents ({','.join(common)}) "
                                f"VALUES ({','.join('?' * len(common))})", rows)
                counts["source_documents"] = len(rows)
            dst.commit()
            out = {"dst": str(dst_path), "src": str(src_path), "sampled": len(qids),
                   "copied": counts, "taxonomy_tags": dst.execute(
                       "SELECT COUNT(*) FROM label_taxonomy WHERE enabled=1").fetchone()[0]}
        except Exception:
            dst.close()
            tmp_path.unlink(missing_ok=True)
            raise
        dst.close()
    finally:
        src.close()

    os.replace(tmp_path, dst_path)        # 同目录原子替换：成功才换掉旧测试库
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
