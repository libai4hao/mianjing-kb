# -*- coding: utf-8 -*-
"""purge_origin：删除"已收入语料"的原件（导入源目录里的副本），删前逐件校验。

删除安全的前提是**存在字节完全一致的保留副本**（完整 sha256 相等），
而不是"记录状态是 classified"。

判据（缺一不删）：
  ① 该文件完整 sha256 在 `source_documents` 里有记录（日志写完整 64 位）
  ② 记录状态是**已入库**（`classified`；`superseded` 还要求有接替件——优先读
     `document_events` 的 converted_to 事件，老库退回 note 里的 `converted_to:`，
     否则默认拒绝，需显式 `--allow-superseded-no-replacement`）
  ③ 记录自己在磁盘上的那份（`src_path`）**真实存在**、落在语料根内、
     不是待删文件自身（`samefile`）、非 0 字节、且**完整 sha256 与待删文件相等**
  ④ 待删文件所在目录在 `--roots` 白名单内，且该根不落在语料根内/不包含语料根
  ⑤ 待删文件非 0 字节

删除链路：先写 `planned` 日志行并 fsync，再删，再追加 `deleted`/`failed` 行；
删除前二次校验 size/mtime 未变、并**重算原文 sha256**（`--no-rehash` 可跳过），
实删数与计划数不一致时退出码 1。

用法：
    python -m mianjing_kb.purge_origin --roots "%RESOURCE_ROOT%\\就业资料" "%DOC_ROOT%\\待分类"          # 只预览
    python -m mianjing_kb.purge_origin --roots ... --apply --yes                                        # 真删
    python -m mianjing_kb.purge_origin --roots ... --apply --yes --max-delete 100                       # 上限保护
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, events, hashing, paths, status, wlock  # noqa: E402

OK_STATUS = status.PURGE_OK


def _has_replacement(conn, doc_hash: str, note: str | None) -> bool:
    """是否有"接替件"：事件表优先（T12），老库没跑迁移时退回 note 子串。"""
    if events.has(conn, doc_hash, events.KIND_CONVERTED_TO):
        return True
    return any(k == events.KIND_CONVERTED_TO for k, _ in events.parse_note(note))


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _within(child: Path, parent: Path) -> bool:
    try:
        _resolved(child).relative_to(_resolved(parent))
        return True
    except ValueError:
        return False


def _in_quarantine(p: Path, corpus: Path) -> bool:
    """99-重复文件待删 里的副本绝不能当"保留副本"——它本身就是待删件。"""
    try:
        rel = p.resolve().relative_to(corpus.resolve()).as_posix()
    except (ValueError, OSError):
        return False
    q = config.QUARANTINE_PREFIX
    return rel == q or rel.startswith(q + "/")


def _root_refusal(root: Path, corpus: Path) -> str | None:
    """根目录合法性：过宽（盘符根 / 用户主目录）或与语料根重叠一律拒绝。

    重叠是真实的数据丢失路径：根在语料内时，每个文件的登记 src_path 就是它自己，
    会被当成"保留副本"通过校验，导致整棵树被计划删除。
    """
    rr = _resolved(root)
    if rr == Path(rr.anchor):
        return "不允许盘符根目录"
    if rr == _resolved(Path.home()):
        return "不允许用户主目录"
    cr = _resolved(corpus)
    if rr == cr:
        return "根目录就是语料根"
    if _within(rr, cr):
        return "根目录在语料根内"
    if _within(cr, rr):
        return "根目录包含语料根"
    return None


def _walk_skip(root: Path, forbidden: list[Path], on_error):
    def _prune(dirnames: list[str], dirpath: str) -> None:
        # junction/软链目录也剪掉：purge 会删文件，顺着链接走进别处就是误删
        dirnames[:] = [d for d in dirnames
                       if not any(_within(Path(dirpath) / d, f) for f in forbidden)
                       and not paths.is_link(Path(dirpath) / d)]

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        _prune(dirnames, dirpath)
        for fn in filenames:
            p = Path(dirpath) / fn
            if paths.is_link(p) or not paths.resolves_inside(root, p):
                continue                    # 链接目标/根外文件不进入删除计划
            yield p


LOG_HEADER = ["deleted_at", "origin_path", "size_bytes", "doc_hash", "kept_copy", "status"]


def _log_path(log: Path) -> Path:
    """老日志是 5 列（没有 status），直接追加 6 列行会把 CSV 撑成锯齿状。

    表头不一致时改写到同目录的 `<stem>_v2<suffix>`，原日志一个字节都不动。
    """
    if not log.exists():
        return log
    try:
        first = log.read_text(encoding="utf-8-sig").splitlines()[:1]
    except OSError:
        first = []
    if first and [c.strip() for c in first[0].split(",")] == LOG_HEADER:
        return log
    alt = log.with_name(f"{log.stem}_v2{log.suffix}")
    print(f"[warn] {log.name} 是旧表头，本次明细改写 {alt.name}（旧文件保持原样）", file=sys.stderr)
    return alt


def _parse(argv=None):
    ap = argparse.ArgumentParser(description="删除已收入语料的原件")
    ap.add_argument("--roots", nargs="+", required=True, help="导入源目录白名单")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--log", default=str(config.BUILD_DIR / "origin_deleted.csv"))
    ap.add_argument("--apply", action="store_true", help="真删（否则只预览）")
    ap.add_argument("--yes", action="store_true",
                    help="与 --apply 同时给才执行（高危：物理删除原件，缺 --yes 只打印计划）")
    ap.add_argument("--rmdirs", action="store_true", help="清掉本次删除后才变空的目录")
    ap.add_argument("--max-delete", type=int, default=0,
                    help="计划删除数超过 N 件时拒绝 --apply（0 = 不设上限）")
    ap.add_argument("--allow-superseded-no-replacement", action="store_true",
                    help="放行没有接替件的 superseded 记录（事件表与 note 都查不到 converted_to）")
    ap.add_argument("--no-rehash", action="store_true",
                    help="删除前不重算原文 sha256（快一倍，但会把'扫描后内容被换掉'的窗口留开）")
    ap.add_argument("--no-backup", action="store_true",
                    help="--apply 前不备份 kb.sqlite（默认备份到 <db>/backups，保留 5 份）")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    # 缺 --yes 在 _main_locked 内拒绝，不持锁也不动盘。
    if not (args.apply and args.yes):
        return _main_locked(args)
    try:
        with wlock.write_lock("kb", tool="purge_origin",
                              build_dir=Path(args.db).parent):
            return _main_locked(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另一个写库工具在跑；等它结束再重试"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def _main_locked(args) -> int:
    refused = False
    if args.apply and not args.yes:
        print("[refuse] --apply 必须配 --yes：这是物理删除，缺一步就只打印计划", file=sys.stderr)
        args.apply = False
        refused = True

    corpus = Path(config.CORPUS_ROOT)
    roots = [Path(os.path.abspath(r)) for r in args.roots]
    for r in roots:
        if not r.is_dir():
            print(f"[fatal] 不是目录：{r}", file=sys.stderr)
            return 2
        why = _root_refusal(r, corpus)
        if why:
            print(f"[refuse] {r}：{why}（与语料根重叠会删掉唯一副本）", file=sys.stderr)
            return 2

    skipped_dirs: list[tuple[str, str]] = []

    def _onerror(err: OSError) -> None:
        skipped_dirs.append((getattr(err, "filename", "") or "?", "读失败目录"))

    if args.apply and not args.no_backup:
        backup = db.backup_db(args.db)
        print(f"[backup] 删除前已备份账本：{backup}", file=sys.stderr)

    conn = db.connect(args.db, create=False, readonly=True)
    forbidden = [corpus, Path(config.BUILD_DIR)]
    plan, kept = [], list(skipped_dirs)
    no_repl = 0
    # 保留副本哈希按路径缓存：大量重复件会指向同一个保留副本，重组一次即可；
    # 扫描期间不改语料，缓存到本次运行结束（删除前只重算待删件本身）。
    keep_hashes: dict[Path, str] = {}

    def keep_hash(p: Path) -> str:
        if p not in keep_hashes:
            keep_hashes[p] = hashing.sha256_file(p)
        return keep_hashes[p]
    for root in roots:
        for p in _walk_skip(root, forbidden, _onerror):
            try:
                st = p.stat()
            except OSError as ex:
                kept.append((str(p), f"读失败:{type(ex).__name__}"))
                continue
            if st.st_size == 0:
                kept.append((str(p), "空文件"))
                continue
            try:
                digest = hashing.sha256_file(p)
            except OSError as ex:
                kept.append((str(p), f"读失败:{type(ex).__name__}"))
                continue
            rows = conn.execute(
                "SELECT doc_hash, sha256, status, src_path, file_name, note FROM source_documents "
                "WHERE doc_hash=?",
                (digest[:16],),
            ).fetchall()
            # 16 位短指纹只用于定位行；是否“已收入”必须由**完整 sha256**背书。
            # 撞车时宁可保留（拒绝删除），也不能把另一份内容的登记行当成本件。
            rows = [r for r in rows if r["sha256"] == digest]
            if not rows:
                kept.append((str(p), "未收入（短指纹无记录或完整 sha256 不符）"))
                continue
            # 同一 hash 可能有多行（隔离副本 / 99 区待删副本）；只有出现"已入库"行才算收入
            ok_rows = [r for r in rows if r["status"] in OK_STATUS]
            if not ok_rows:
                kept.append((str(p), "只有隔离副本:" + "/".join(sorted({r["status"] for r in rows}))))
                continue
            bad_repl = [r for r in ok_rows if r["status"] == status.SUPERSEDED
                        and not _has_replacement(conn, r["doc_hash"], r["note"])]
            if bad_repl and not args.allow_superseded_no_replacement:
                no_repl += 1
                kept.append((str(p), "superseded 无接替件（需 --allow-superseded-no-replacement）"))
                continue
            keep, reason = None, "保留副本不在语料根内或不存在"
            for r in ok_rows:
                cand = Path(r["src_path"])
                if not _within(cand, corpus):
                    continue
                if _in_quarantine(cand, corpus):
                    reason = "保留副本落在 99-重复文件待删（拒绝当保留副本）"
                    continue
                if not cand.is_file():
                    reason = "保留副本不存在"
                    continue
                if cand.stat().st_size == 0:
                    reason = "保留副本为空"
                    continue
                try:
                    if os.path.samefile(cand, p):
                        reason = "keep copy is self"
                        continue
                except OSError:
                    reason = "保留副本不存在"
                    continue
                if keep_hash(cand) != digest:
                    reason = "keep copy content mismatch"
                    continue
                keep = cand
                break
            if keep is None:
                kept.append((str(p), reason))
                continue
            plan.append((p, digest, keep, st.st_size, st.st_mtime_ns))

    print(json.dumps({
        "roots": [str(r) for r in roots],
        "planned_delete": len(plan),
        "planned_bytes": sum(x[3] for x in plan),
        "kept": len(kept),
        "kept_reasons": {rr: sum(1 for _p, r2 in kept if r2 == rr) for _p, rr in kept},
        "superseded_no_replacement": no_repl,
        "dry_run": not args.apply,
    }, ensure_ascii=False, indent=2))
    for p, _d, keep, _s, _m in plan[:6]:
        print(f"   删除 {p}  ←保留 {keep}")
    if not args.apply:
        conn.close()
        return 2 if refused else 0

    if args.max_delete and len(plan) > args.max_delete:
        print(f"[refuse] 计划删除 {len(plan)} 件 > --max-delete {args.max_delete}，"
              "确认后重跑或调大上限", file=sys.stderr)
        conn.close()
        return 2

    log = _log_path(Path(args.log))
    log.parent.mkdir(parents=True, exist_ok=True)
    new_log = not log.exists()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    deleted, emptied = 0, set()
    failures = 0
    with log.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(LOG_HEADER)

        def row(status: str, p: Path, size: int, digest: str, keep: Path) -> None:
            w.writerow([now, str(p), size, digest, str(keep), status])
            fh.flush()
            os.fsync(fh.fileno())

        for p, digest, keep, size, mtime_ns in plan:
            # 先落盘意图，再动手：日志先于 unlink，断电/崩溃时不会出现"删了但没记录"
            row("planned", p, size, digest, keep)
            try:
                st = p.stat()
                if st.st_size != size or st.st_mtime_ns != mtime_ns:
                    raise OSError("扫描后文件已变化")
                if not keep.is_file():
                    raise OSError("保留副本已不存在")
                # 收窗：扫描到删除之间文件可能被改写（同 size+同 mtime 也能换内容），删前重算一次
                if not args.no_rehash and hashing.sha256_file(p) != digest:
                    raise OSError("删除前内容已变化（重算 sha256 不符）")
                p.unlink()
            except OSError as ex:
                failures += 1
                print(f"[warn] 删除失败 {p}: {ex}", file=sys.stderr)
                row("failed", p, size, digest, keep)
                continue
            deleted += 1
            emptied.add(p.parent)
            row("deleted", p, size, digest, keep)

    if args.rmdirs:
        removed = 0
        for d in sorted(emptied, key=lambda x: len(x.parts), reverse=True):
            cur = d
            while cur != cur.parent and any(_within(cur, r) for r in roots):
                try:
                    if not os.listdir(cur):        # 只清本次删空的目录，不碰原本就空的
                        os.rmdir(cur)
                        removed += 1
                        cur = cur.parent
                        continue
                except OSError:
                    pass
                break
        print(f"[rmdir] 清掉本次删空的目录 {removed} 个")

    conn.close()
    print(f"[done] 已删 {deleted}/{len(plan)} 件，明细 → {log}")
    return 0 if deleted == len(plan) else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
