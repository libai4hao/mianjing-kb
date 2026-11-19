# -*- coding: utf-8 -*-
"""intake：把磁盘上的文件登记进"原文档表"。

职责边界（docs/03 §10.3 §10.4）：
  - 只做"登记 + 判重"，不做分类、不解析；
  - 一级判重靠 sha256（实测 444 件 2.93 s，单件 6.6 ms），一次读取同时得到短指纹与全量值；
  - 命中已有记录时区分动作：duplicate / moved / renamed / resurrected；
  - 99-重复文件待删 里的文件照样登记，但状态直接 quarantined，永不参与处理。

2026-11-19 二审（T11/T12/T15）：
  - 分块提交：每 500 件 commit 一次（`CHUNK_SIZE`），30 分钟扫描中途崩溃不再整批丢失；
    批次行先写 `state=running`，正常结束写 completed，异常写 failed，重扫即可续（判重幂等）；
  - 跨进程写锁：整趟扫描持有 `<build>/locks/kb.lock`，第二个写库工具直接拒绝，
    而不是等 10 秒 busy_timeout 后吐一句 `database is locked`；
  - note 双写事件：`replaced_by:` / `moved_from:` 等继续写 note 留痕，同时落
    `document_events`，读方（purge / verify G5）不再依赖子串。

用法：
    python -m mianjing_kb.intake --dry-run                 # 只看统计（有库时也报重复数），不写库
    python -m mianjing_kb.intake                           # 登记语料根
    python -m mianjing_kb.intake --root "<DOWNLOAD_DIR>\\面试资料"   # 登记语料树之外的目录（示例路径，按本机实际替换）
    python -m mianjing_kb.intake --all-exts                # 连图片/xmind 也登记（默认只登记文档型）
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, events, hashing, status, wlock  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
CHUNK_SIZE = 500          # 每 N 件提交一次：崩溃损失上限 + 写锁持有粒度


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


def _is_reparse_dir(path: str) -> bool:
    """Windows 目录重解析点（junction/符号链接）：os.walk 会进入 junction，
    同一份文件会在两条路径下重复登记。3.12 才有 isjunction，低版本看属性位。"""
    if sys.platform != "win32":
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    flag = getattr(st, "st_file_attributes", 0)
    return bool(flag & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def iter_files(root: Path, all_exts: bool):
    """遍历所有文件，产出 (path, rel, ext, size|None, quarantined)。

    同内容多副本时，登记顺序决定"谁占主记录"：必须让正式区先于
    99-重复文件待删，且同优先级按 rel 字典序稳定。否则 os.walk 的目录顺序
    会让隔离区副本抢占正式件身份（ISSUE-01）。
    """
    base = scan_base(root)
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 不跟随 junction/重解析点目录，避免同内容在两条路径重复注册；
        # 目录显式排序，保证遍历本身可复现（最终还会按 quarantined/rel 再排一次）。
        dirnames[:] = sorted(
            (d for d in dirnames
             if not _is_reparse_dir(os.path.join(dirpath, d))),
            key=lambda d: (is_quarantine(rel_posix(Path(dirpath) / d, base)), d.casefold()))
        rel_dir = rel_posix(Path(dirpath), base)
        if is_excluded(rel_dir):
            dirnames[:] = []
            continue
        quarantined = is_quarantine(rel_dir)
        for fn in sorted(filenames, key=str.casefold):
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            rel = rel_posix(p, base)
            if not all_exts and ext not in config.DOC_EXTS:
                entries.append((quarantined, rel.casefold(), p, rel, ext, None, quarantined))
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            entries.append((quarantined, rel.casefold(), p, rel, ext, size, quarantined))
    # 正式区（quarantined=False）整体排在隔离区之前；同区按相对路径稳定排序。
    for _q, _rel_key, p, rel, ext, size, quarantined in sorted(
            entries, key=lambda x: (x[0], x[1])):
        yield p, rel, ext, size, quarantined


def _src_is_original(row, rel: str | None = None) -> bool:
    """登记路径上的文件现在是否仍是这条记录的内容（按完整 sha256，不只看存在）。"""
    sp = row["src_path"]
    if not sp:
        return False
    if rel and sp.replace("\\", "/").endswith("/" + rel.replace("\\", "/")):
        return True          # 就是当前扫到的这份，hash 已匹配，无需重读
    try:
        return Path(sp).is_file() and hashing.sha256_file(sp) == row["sha256"]
    except OSError:
        return False


def decide(conn, doc_hash: str, rel: str, name: str, quarantined: bool = False,
           sha256: str | None = None) -> tuple[str, str]:
    """返回 (action, status_after)。

    关键约束：移动 / 改名**不能**改变已有状态（否则 processed 会被降级成 moved，
    已入库的成果就"回滚"了）。只有"曾删除又回来"才允许重置状态。
    隔离区里的文件即使内容与库内相同，也只算重复，不能把库内记录的路径改到隔离区。
    但反过来：正式区的记录被**移进**隔离区（过时件清理）时，必须改路径并转 quarantined。
    原件仍在磁盘上时只算重复——否则往收件箱丢一份副本，会把库里那条记录的位置改走。
    """
    row = db.get_by_hash(conn, doc_hash)
    if row is None:
        return "added", status.PENDING
    # 16 位短指纹撞车时绝不覆盖已有行：完整 sha256 不同就按异常处理
    if sha256 and row["sha256"] and row["sha256"] != sha256:
        return "hash_collision", row["status"]
    prev = row["status"]
    if prev == status.ABSENT:
        # 先判隔离：99 区里的文件永远不能借"恢复上线"洗成正式语料
        if quarantined:
            return "to_quarantine", status.QUARANTINED
        # 曾删除又回到正式区：复用原结果，不能被当成普通重复件
        return "resurrected", (status.CLASSIFIED if row["category"] else status.PENDING)
    full_rel = f"{row['dir_path']}/{row['file_name']}" if row["dir_path"] else row["file_name"]
    if quarantined:
        if full_rel == rel:
            return "duplicate", prev
        if _src_is_original(row, rel):
            return "duplicate", prev          # 原件还在正式区 → 99 里的只是散副本，不能夺主
        # 原件已被移进隔离区（过时件清理走这条路）：改路径并把状态转 quarantined
        return "to_quarantine", status.QUARANTINED
    if prev == status.QUARANTINED and not quarantined:
        # 隔离区（99-重复文件待删）里的副本只是"待删副本"，不是可靠来源。
        # 正式区又见到同内容 → 内容要回正式区重新走分类，否则"库里只有隔离副本"的件
        # 永远不会进库（曾踩：Spring系列面试题129道 只判 duplicate，既不导入也不判重）。
        return "revived", status.PENDING
    if full_rel == rel:
        return "duplicate", prev
    if _src_is_original(row, rel):
        return "duplicate", prev              # 登记路径上的内容还在 → 这次见到的是副本
    new_dir = dir_of(rel)
    old_dir = row["dir_path"] or ""
    if row["file_name"] != name:
        return ("renamed" if new_dir == old_dir else "moved"), prev
    return "moved", prev


def ghost_candidates(conn, dir_path: str, file_name: str, new_hash: str,
                     new_path: Path) -> list[str]:
    """收集"同路径换内容"的待退役旧行 hash；真正退役推迟到扫描结束。

    延迟是必要的：扫描到被覆盖的 A/x.pdf 时，旧内容可能还没在 B/x.pdf 被扫到，
    立刻退役会把仍在别处有效的记录打成 superseded（评审 2026-11-19 高风险#2）。
    只收集"旧路径文件已不存在 / 正被当前新文件覆盖"的行；若旧记录内容还完整
    存在于另一条路径，说明只是复制/迁移，不能退役。
    """
    rows = conn.execute(
        "SELECT doc_hash, sha256, src_path FROM source_documents "
        "WHERE dir_path=? AND file_name=? AND doc_hash != ? "
        f"AND status NOT IN {status.sql_in((status.ABSENT, status.SUPERSEDED))}",
        (dir_path, file_name, new_hash),
    ).fetchall()
    out = []
    for r in rows:
        sp = r["src_path"]
        if sp:
            try:
                if os.path.samefile(sp, new_path):
                    pass                   # 同一路径原地覆盖：正是要退役的幽灵
                elif Path(sp).exists():
                    if hashing.sha256_file(sp) == r["sha256"]:
                        continue           # 别处字节未变 → 有效副本，交给 moved/duplicate
            except OSError:
                pass                       # 读不到/不存在 → 允许退役
        out.append(r["doc_hash"])
    return out


def retire_hash(conn, old_hash: str, new_hash: str) -> int:
    """把单条旧行转 superseded，保留原 note 留痕，同时记 replaced_by 事件。"""
    cur = conn.execute(
        f"UPDATE source_documents SET status='{status.SUPERSEDED}', "
        "note='replaced_by:' || ? || COALESCE(' | ' || note, '') "
        f"WHERE doc_hash=? AND status NOT IN {status.sql_in((status.ABSENT, status.SUPERSEDED))}",
        (new_hash, old_hash),
    )
    if cur.rowcount:
        events.append(conn, old_hash, events.KIND_REPLACED_BY, {"hash": new_hash})
    return cur.rowcount


def _batch_counts(actions: Counter) -> dict:
    return {
        "added": actions["added"],
        "duplicated": actions["duplicate"],
        "moved": actions["moved"] + actions["renamed"] + actions["resurrected"],
        "quarantined": actions["quarantined"],
        "ignored": actions["ignored"],
    }


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

    batch = args.batch or (datetime.now().strftime("%Y%m%d-%H%M%S") + "-"
                           + secrets.token_hex(3) + "-intake")
    stack = ExitStack()
    write_conn = None
    read_conn = None
    if not args.dry_run:
        try:
            stack.enter_context(wlock.write_lock("kb", tool="intake",
                                                 build_dir=Path(args.db).parent))
        except wlock.LockHeld as ex:
            print(json.dumps({"verdict": "ABORT", "why": str(ex),
                              "note": "另一个写库工具在跑；等它结束再重试，不要手工删锁"},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return 2

    actions: Counter = Counter()
    by_ext: Counter = Counter()
    seen_hashes: dict[str, str] = {}   # 本次扫描内存判重：dry-run 与真实运行的重复数对齐
    pending_ghosts: list[tuple[str, str]] = []   # (旧 hash, 新 hash)，扫完再退役
    started = time.perf_counter()
    pending_writes = 0

    def flush_batch(state: str | None = None) -> None:
        """提交并刷新批次进度（每 CHUNK_SIZE 件一次；final=True 时写结束状态）。"""
        nonlocal pending_writes
        if write_conn is None:
            return
        db.update_batch(write_conn, batch,
                        state=state or db.BATCH_RUNNING, **_batch_counts(actions))
        write_conn.commit()
        pending_writes = 0

    try:
        if args.dry_run:
            db_file = Path(args.db)
            if db_file.exists():                       # 只读打开，用于报出真实重复数
                read_conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
                read_conn.row_factory = sqlite3.Row
            conn = read_conn
        else:
            db_existed = Path(args.db).exists()
            write_conn = db.connect(args.db)
            conn = write_conn
            if db_existed:
                db.backup_db(args.db)   # 写库前先热备份事实源（账本/金标不可重建）
            # 批次先落 running：中途崩溃时库里有"这批跑到哪"，重扫可据判重幂等续上
            db.start_batch(write_conn, batch_id=batch, root=str(root), mode="full")
            write_conn.commit()

        for p, rel, ext, size, quarantined in iter_files(root, args.all_exts):
            if size is None:
                actions["ignored"] += 1
                by_ext[ext] += 1
                continue
            try:
                sha, size = hashing.sha256_file_sized(p)   # size 取自同一遍读取，避免 stat/hash 竞态
            except OSError as ex:
                print(f"[warn] 读取失败 {rel}: {ex}", file=sys.stderr)
                actions["error"] += 1
                continue
            doc_hash = sha[:16]
            if conn is None:
                actions["candidate"] += 1
                continue
            if doc_hash in seen_hashes:
                # 同一批内重复：dry-run 不会写库，必须靠内存 seen 才能给出真实重复数
                actions["duplicate"] += 1
                continue
            seen_hashes[doc_hash] = rel
            action, status_after = decide(conn, doc_hash, rel, p.name, quarantined, sha256=sha)
            if action == "hash_collision":
                print(f"[warn] {rel}: 16 位短指纹撞上不同内容，跳过（需人工核对）", file=sys.stderr)
                actions["hash_collision"] += 1
                continue
            if quarantined and action == "added":
                action, status_after = "quarantined", status.QUARANTINED
            actions[action] += 1
            if args.dry_run:
                continue

            if action in ("added", "quarantined"):
                pending_ghosts.extend(
                    (h, doc_hash) for h in ghost_candidates(
                        conn, dir_of(rel), p.name, doc_hash, p))
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
                if ext in config.NEEDS_CONVERT_EXTS:
                    events.append(conn, doc_hash, events.KIND_NEEDS_CONVERT)
                added_n = 1
            else:
                added_n = 0
                now = datetime.now().strftime(NOW_FMT)
                if action in ("moved", "renamed", "resurrected", "to_quarantine", "revived"):
                    row = db.get_by_hash(conn, doc_hash)
                    # *_from 记**旧**路径（改前），否则"从哪来"记成"到哪去"，复核时读反
                    old_rel = (f"{row['dir_path']}/{row['file_name']}" if row["dir_path"]
                               else row["file_name"]) if row else rel
                    old_src = row["src_path"] if action == "revived" else None
                    note = (f"revived_from:{old_src}" if action == "revived"
                            else f"{action}_from:{old_rel}")
                    write_conn.execute(
                        "UPDATE source_documents SET dir_path=?, parent_dir=?, file_name=?, src_path=?, "
                        "last_seen_at=?, note=CASE WHEN note IS NULL OR note='' THEN ? "
                        "  ELSE ? || ' | ' || note END WHERE doc_hash=?",
                        (dir_of(rel), parent_of(rel), p.name, str(p), now, note, note, doc_hash),
                    )
                    events.append(conn, doc_hash, f"{action}_from",
                                  {"rel": old_src if action == "revived" else old_rel})
                    # F7 补遗（评审 Important#1，2026-11-19）：行被（重）指到新路径时，
                    # 该路径上不同 hash 的占用行同样要退役——移动/复制覆盖与原地改
                    # 内容是同一故障模式（doc_pool 同路径双份抽题 + hash 绑错内容）。
                    # 同样延迟到扫描结束：占用行的有效副本可能还没被扫到。
                    pending_ghosts.extend(
                        (h, doc_hash) for h in ghost_candidates(
                            conn, dir_of(rel), p.name, doc_hash, p))
                else:
                    # duplicate：只更新"最后见到"，绝不改路径。
                    # 否则隔离区/别处的重名件会把库内记录的位置改走（曾踩过）。
                    write_conn.execute(
                        "UPDATE source_documents SET last_seen_at=? WHERE doc_hash=?", (now, doc_hash)
                    )
                if action in ("resurrected", "to_quarantine", "revived"):
                    write_conn.execute("UPDATE source_documents SET status=? WHERE doc_hash=?",
                                       (status_after, doc_hash))
            pending_writes += 1 + added_n
            if pending_writes >= CHUNK_SIZE:
                flush_batch()

        if write_conn is not None:
            # 扫描结束才退役：旧内容若在本批另一条路径被见到，它的记录已走 moved 更新，
            # 不能再打成 superseded（否则有效内容从所有清单里静默消失）。
            for old_hash, new_hash in pending_ghosts:
                if old_hash in seen_hashes:
                    continue
                actions["ghost_retired"] += retire_hash(write_conn, old_hash, new_hash)
            flush_batch(state=db.BATCH_COMPLETED)
    except Exception as ex:                            # noqa: BLE001
        # 已提交的分块保留（下次重扫按 hash 幂等续）；先把批次打成 failed 再退出
        if write_conn is not None:
            try:
                db.update_batch(write_conn, batch, state=db.BATCH_FAILED,
                                note=f"{type(ex).__name__}: {str(ex)[:120]}",
                                **_batch_counts(actions))
                write_conn.commit()
            except Exception as ex2:                   # noqa: BLE001
                print(f"[warn] 批次状态写入失败: {ex2}", file=sys.stderr)
        print(f"[fatal] 扫描中断（已提交的分块保留，重跑可续）：{type(ex).__name__}: {ex}",
              file=sys.stderr)
        return 3
    finally:
        for c in (write_conn, read_conn):
            if c is not None:
                c.close()
        stack.close()

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
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
