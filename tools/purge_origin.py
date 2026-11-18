# -*- coding: utf-8 -*-
"""purge_origin：删除"已收入语料"的原件（导入源目录里的副本），删前逐件校验。

删除安全的前提是**存在字节完全一致的保留副本**（完整 sha256 相等），
而不是"记录状态是 classified"。

判据（缺一不删）：
  ① 该文件完整 sha256 在 `source_documents` 里有记录（日志写完整 64 位）
  ② 记录状态是**已入库**（`classified`；`superseded` 还要求 note 里有 `converted_to:`，
     否则默认拒绝，需显式 `--allow-superseded-no-replacement`）
  ③ 记录自己在磁盘上的那份（`src_path`）**真实存在**、落在语料根内、
     不是待删文件自身（`samefile`）、非 0 字节、且**完整 sha256 与待删文件相等**
  ④ 待删文件所在目录在 `--roots` 白名单内，且该根不落在语料根内/不包含语料根
  ⑤ 待删文件非 0 字节

删除链路：先写 `planned` 日志行并 fsync，再删，再追加 `deleted`/`failed` 行；
删除前二次校验 size/mtime 未变；实删数与计划数不一致时退出码 1。

用法：
    python tools/purge_origin.py --roots "%RESOURCE_ROOT%\\就业资料" "%DOC_ROOT%\\待分类"          # 只预览
    python tools/purge_origin.py --roots ... --apply --yes                                        # 真删
    python tools/purge_origin.py --roots ... --apply --yes --max-delete 100                       # 上限保护
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
from kb import config, db, hashing  # noqa: E402

OK_STATUS = ("classified", "superseded")


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
        dirnames[:] = [d for d in dirnames
                       if not any(_within(Path(dirpath) / d, f) for f in forbidden)]

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        _prune(dirnames, dirpath)
        for fn in filenames:
            yield Path(dirpath) / fn


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


def main(argv=None) -> int:
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
                    help="放行 note 里没有 converted_to: 的 superseded 记录（默认拒绝）")
    args = ap.parse_args(argv)
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

    conn = db.connect(args.db, create=False, readonly=True)
    forbidden = [corpus, Path(config.BUILD_DIR)]
    plan, kept = [], list(skipped_dirs)
    no_repl = 0
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
                "SELECT doc_hash, status, src_path, file_name, note FROM source_documents "
                "WHERE doc_hash=?",
                (digest[:16],),
            ).fetchall()
            if not rows:
                kept.append((str(p), "未收入"))
                continue
            # 同一 hash 可能有多行（隔离副本 / 99 区待删副本）；只有出现"已入库"行才算收入
            ok_rows = [r for r in rows if r["status"] in OK_STATUS]
            if not ok_rows:
                kept.append((str(p), "只有隔离副本:" + "/".join(sorted({r["status"] for r in rows}))))
                continue
            bad_repl = [r for r in ok_rows if r["status"] == "superseded"
                        and "converted_to:" not in (r["note"] or "")]
            if bad_repl and not args.allow_superseded_no_replacement:
                no_repl += 1
                kept.append((str(p), "superseded 无接替件（需 --allow-superseded-no-replacement）"))
                continue
            keep, reason = None, "保留副本不在语料根内或不存在"
            for r in ok_rows:
                cand = Path(r["src_path"])
                if not _within(cand, corpus):
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
                if hashing.sha256_file(cand) != digest:
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
    raise SystemExit(main())
