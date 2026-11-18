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


def parent_of(rel: str) -> str:
    """父目录名（最后一级目录）；根目录直属的记为 <顶层直属>。"""
    d = dir_of(rel)
    if not d:
        return "<顶层直属>"
    return d.rsplit("/", 1)[-1]


def is_excluded(rel_dir: str) -> bool:
    """按任意层级排除：00-索引 / 20-题库MD / 21-面经数据库 出现在哪一层都不算源文档
    （否则 99 区镜像的派生 md 会被当成新源重新登记）。"""
    parts = rel_dir.split("/") if rel_dir else []
    if any(p in config.EXCLUDE_DIR_NAMES for p in parts):
        return True
    return any(rel_dir == p or rel_dir.startswith(p + "/") for p in config.EXCLUDE_PREFIXES)


def is_quarantine(rel_dir: str) -> bool:
    q = config.QUARANTINE_PREFIX
    return rel_dir == q or rel_dir.startswith(q + "/")


def scan_base(root: Path) -> Path:
    """路径一律相对**语料根**登记，即使只扫子目录（如 31-收件箱）也不例外。

    否则 dir_path 会变成 "面经/2026-11-17-.../阿里/..."，丢掉 31-收件箱 前缀，
    folder_rules 里 31-收件箱/面经/** 这类结构位规则永远匹配不上 → 全判未分类。
    根在语料之外（外部目录直接导入）时退回自身。
    """
    try:
        root.relative_to(config.CORPUS_ROOT)
        return Path(config.CORPUS_ROOT)
    except ValueError:
        return root


def iter_files(root: Path, all_exts: bool):
    """遍历所有文件，产出 (path, rel, ext, size|None, quarantined)。"""
    base = scan_base(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = rel_posix(Path(dirpath), base)
        if is_excluded(rel_dir):
            dirnames[:] = []
            continue
        quarantined = is_quarantine(rel_dir)
        for fn in filenames:
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if not all_exts and ext not in config.DOC_EXTS:
                yield p, rel_posix(p, base), ext, None, quarantined
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            yield p, rel_posix(p, base), ext, size, quarantined


def decide(conn, doc_hash: str, rel: str, name: str, quarantined: bool = False) -> tuple[str, str]:
    """返回 (action, status_after)。

    关键约束：移动 / 改名**不能**改变已有状态（否则 processed 会被降级成 moved，
    已入库的成果就"回滚"了）。只有"曾删除又回来"才允许重置状态。
    隔离区里的文件即使内容与库内相同，也只算重复，不能把库内记录的路径改到隔离区。
    但反过来：正式区的记录被**移进**隔离区（过时件清理）时，必须改路径并转 quarantined。
    原件仍在磁盘上时只算重复——否则往收件箱丢一份副本，会把库里那条记录的位置改走。
    """
    row = db.get_by_hash(conn, doc_hash)
    if row is None:
        return "added", "pending_classify"
    prev = row["status"]
    if prev == "absent":
        # 先判"曾删除又回来"：这类文件必须复用原结果，不能被当成普通重复件
        return "resurrected", ("classified" if row["category"] else "pending_classify")
    full_rel = f"{row['dir_path']}/{row['file_name']}" if row["dir_path"] else row["file_name"]
    src = row["src_path"]
    if quarantined:
        if full_rel == rel:
            return "duplicate", prev
        if src and Path(src).exists():
            return "duplicate", prev          # 原件还在正式区 → 99 里的只是散副本，不能夺主
        # 原件已被移进隔离区（过时件清理走这条路）：改路径并把状态转 quarantined
        return "to_quarantine", "quarantined"
    if prev == "quarantined" and not quarantined:
        # 隔离区（99-重复文件待删）里的副本只是"待删副本"，不是可靠来源。
        # 正式区又见到同内容 → 内容要回正式区重新走分类，否则"库里只有隔离副本"的件
        # 永远不会进库（曾踩：Spring系列面试题129道 只判 duplicate，既不导入也不判重）。
        return "revived", "pending_classify"
    if full_rel == rel:
        return "duplicate", prev
    if src and Path(src).exists():
        return "duplicate", prev              # 原件还在 → 这次见到的是副本
    new_dir = dir_of(rel)
    old_dir = row["dir_path"] or ""
    if row["file_name"] != name:
        return ("renamed" if new_dir == old_dir else "moved"), prev
    return "moved", prev


def retire_path_ghost(conn, dir_path: str, file_name: str, new_hash: str) -> int:
    """F7（2026-11-19）：同路径换内容时退役旧行（幽灵行）。

    intake 只按 hash 判重：同路径新内容判 added 落新行后，旧行（不同 hash）仍
    classified——doc_pool 会同路径双份抽题，G5 只查 src 存在拦不住。旧行转
    superseded + note replaced_by:<new_hash>，不再进任何清单（doc_pool/ingest_queue
    都只取 classified）。返回退役行数。
    """
    cur = conn.execute(
        "UPDATE source_documents SET status='superseded', note='replaced_by:' || ? "
        "WHERE dir_path=? AND file_name=? AND doc_hash != ? "
        "AND status NOT IN ('absent','superseded')",
        (new_hash, dir_path, file_name, new_hash))
    return cur.rowcount


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
            action, status_after = decide(conn, doc_hash, rel, p.name, quarantined)
            if quarantined and action == "added":
                action, status_after = "quarantined", "quarantined"
            actions[action] += 1
            if args.dry_run:
                continue

            if action in ("added", "quarantined"):
                n_ghost = retire_path_ghost(conn, dir_of(rel), p.name, doc_hash)
                if n_ghost:
                    actions["ghost_retired"] += n_ghost
                row = {
                    "doc_hash": doc_hash,
                    "sha256": sha,
                    "file_name": p.name,
                    "ext": ext,
                    "size_bytes": size,
                    "src_path": str(p),
                    "dir_path": dir_of(rel),
                    "parent_dir": parent_of(rel),
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
                now = datetime.now().strftime(NOW_FMT)
                if action in ("moved", "renamed", "resurrected", "to_quarantine", "revived"):
                    old_src = db.get_by_hash(conn, doc_hash)["src_path"] if action == "revived" else None
                    note = (f"revived_from:{old_src}" if action == "revived"
                            else f"{action}_from:{rel}")
                    write_conn.execute(
                        "UPDATE source_documents SET dir_path=?, parent_dir=?, file_name=?, src_path=?, "
                        "last_seen_at=?, note=? WHERE doc_hash=?",
                        (dir_of(rel), parent_of(rel), p.name, str(p), now, note, doc_hash),
                    )
                else:
                    # duplicate：只更新"最后见到"，绝不改路径。
                    # 否则隔离区/别处的重名件会把库内记录的位置改走（曾踩过）。
                    write_conn.execute(
                        "UPDATE source_documents SET last_seen_at=? WHERE doc_hash=?", (now, doc_hash)
                    )
                if action in ("resurrected", "to_quarantine", "revived"):
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
