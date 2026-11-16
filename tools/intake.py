# -*- coding: utf-8 -*-
"""intake：把磁盘上的文件登记进"原文档表"。

职责边界（docs/03 §10.3 §10.4）：
  - 只做"登记 + 判重"，不做分类、不解析；
  - 一级判重靠 sha256（实测 444 件 2.93 s，单件 6.6 ms），一次读取同时得到短指纹与全量值；
  - 命中已有记录时区分动作：duplicate / moved / renamed / resurrected；
  - 99-重复文件待删 里的文件照样登记，但状态直接 quarantined，永不参与处理。

用法：
    python tools/intake.py --dry-run                 # 只看统计（有库时也报重复数），不写库
    python tools/intake.py                           # 登记语料根
    python tools/intake.py --root "<DOWNLOAD_DIR>\\面试资料"   # 登记语料树之外的目录
    python tools/intake.py --all-exts                # 连图片/xmind 也登记（默认只登记文档型）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db, hashing  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def dir_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def is_excluded(rel_dir: str) -> bool:
    return any(rel_dir == p or rel_dir.startswith(p + "/") for p in config.EXCLUDE_PREFIXES)


def is_quarantine(rel_dir: str) -> bool:
    q = config.QUARANTINE_PREFIX
    return rel_dir == q or rel_dir.startswith(q + "/")


def iter_files(root: Path, all_exts: bool):
    """遍历所有文件，产出 (path, rel, ext, size|None, quarantined)。"""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = rel_posix(Path(dirpath), root)
        if is_excluded(rel_dir):
            dirnames[:] = []
            continue
        quarantined = is_quarantine(rel_dir)
        for fn in filenames:
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if not all_exts and ext not in config.DOC_EXTS:
                yield p, rel_posix(p, root), ext, None, quarantined
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            yield p, rel_posix(p, root), ext, size, quarantined


def decide(conn, doc_hash: str, rel: str, name: str) -> tuple[str, str]:
    """返回 (action, status_after)。

    关键约束：移动 / 改名**不能**改变已有状态（否则 processed 会被降级成 moved，
    已入库的成果就"回滚"了）。只有"曾删除又回来"才允许重置状态。
    """
    row = db.get_by_hash(conn, doc_hash)
    if row is None:
        return "added", "pending_classify"
    prev = row["status"]
    if prev == "absent":
        # 先判"曾删除又回来"：这类文件必须复用原结果，不能被当成普通重复件
        return "resurrected", ("classified" if row["category"] else "pending_classify")
    full_rel = f"{row['dir_path']}/{row['file_name']}" if row["dir_path"] else row["file_name"]
    if full_rel == rel:
        return "duplicate", prev
    new_dir = dir_of(rel)
    old_dir = row["dir_path"] or ""
    if row["file_name"] != name:
        return ("renamed" if new_dir == old_dir else "moved"), prev
    return "moved", prev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="登记源文档并做哈希判重")
    ap.add_argument("--root", default=str(config.CORPUS_ROOT), help="扫描根目录")
    ap.add_argument("--db", default=str(config.DB_PATH), help="数据库路径")
    ap.add_argument("--batch", default=None, help="批次 ID，默认自动生成")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--all-exts", action="store_true", help="连非文档型扩展名也登记")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"[fatal] 根目录不存在: {root}", file=sys.stderr)
        return 2

    batch = args.batch or datetime.now().strftime("%Y%m%d-%H%M") + "-intake"
    write_conn = None
    read_conn = None
    if args.dry_run:
        db_file = Path(args.db)
        if db_file.exists():                       # 只读打开，用于报出真实重复数
            read_conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
            read_conn.row_factory = sqlite3.Row
        conn = read_conn
    else:
        write_conn = db.connect(args.db)
        conn = write_conn

    actions = Counter()
    by_ext = Counter()
    added = 0
    started = time.perf_counter()

    try:
        for p, rel, ext, size, quarantined in iter_files(root, args.all_exts):
            if size is None:
                actions["ignored"] += 1
                by_ext[ext] += 1
                continue
            try:
                sha = hashing.sha256_file(p)
            except OSError as ex:
                print(f"[warn] 读取失败 {rel}: {ex}", file=sys.stderr)
                actions["error"] += 1
                continue
            doc_hash = sha[:16]
            if conn is None:
                actions["candidate"] += 1
                continue
            action, status_after = decide(conn, doc_hash, rel, p.name)
            if quarantined and action == "added":
                action, status_after = "quarantined", "quarantined"
            actions[action] += 1

            if action in ("added", "quarantined"):
                row = {
                    "doc_hash": doc_hash,
                    "sha256": sha,
                    "file_name": p.name,
                    "ext": ext,
                    "size_bytes": size,
                    "src_path": str(p),
                    "dir_path": dir_of(rel),
                    "status": status_after,
                    "batch_id": batch,
                    "first_seen_at": datetime.now().strftime(NOW_FMT),
                    "last_seen_at": datetime.now().strftime(NOW_FMT),
                    "note": "needs_convert" if ext in config.NEEDS_CONVERT_EXTS else None,
                }
                # 立即落库：这样同一批里出现的第二份相同文件会被判成 duplicate，
                # 而不是把小批次的重复留到"下次扫描"才发现。
                db.upsert_document(conn, row)
                added += 1
            elif write_conn is not None:
                write_conn.execute(
                    "UPDATE source_documents SET dir_path=?, file_name=?, src_path=?, last_seen_at=?, "
                    "note=CASE WHEN ? IN ('moved','renamed') THEN ? ELSE note END WHERE doc_hash=?",
                    (dir_of(rel), p.name, str(p), datetime.now().strftime(NOW_FMT),
                     action, f"{action}_from:{rel}" if action in ("moved", "renamed") else None, doc_hash),
                )
                if action == "resurrected":
                    write_conn.execute("UPDATE source_documents SET status=? WHERE doc_hash=?", (status_after, doc_hash))

        if write_conn is not None:
            db.record_batch(
                write_conn,
                batch_id=batch,
                created_at=datetime.now().strftime(NOW_FMT),
                root=str(root),
                mode="full",
                added=actions["added"],
                duplicated=actions["duplicate"],
                moved=actions["moved"] + actions["renamed"] + actions["resurrected"],
                quarantined=actions["quarantined"],
                ignored=actions["ignored"],
                note="",
            )
            write_conn.commit()
    finally:
        for c in (write_conn, read_conn):
            if c is not None:
                c.close()

    elapsed = time.perf_counter() - started
    summary = {
        "root": str(root),
        "batch": batch,
        "dry_run": args.dry_run,
        "db": args.db if conn is not None else "(no db, dry count only)",
        "elapsed_sec": round(elapsed, 2),
        "actions": dict(actions),
        "ignored_by_ext": dict(by_ext.most_common(10)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
