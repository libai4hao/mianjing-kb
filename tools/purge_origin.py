# -*- coding: utf-8 -*-
"""purge_origin：删除"已收入语料"的原件（导入源目录里的副本），删前逐件校验。

判据（缺一不删）：
  ① 该文件 sha256 前 16 位在 `source_documents` 里有记录
  ② 记录状态是**已入库**（`classified` 或 `superseded`；superseded 只认状态，
     不校验 note 里的接替件——接替件缺失会记进 `superseded_no_replacement` 计数并照常列出）
  ③ 记录自己在磁盘上的那份（`src_path`）**真实存在**，且落在语料根内
  ④ 待删文件所在目录在 `--roots` 白名单内
全部通过才删，并把 (源、hash、保留副本) 写进 `build/origin_deleted.csv`。

注意：③ 才是真正的兜底——删的永远是"另有保留副本"的重复件；② 的 superseded 免检接替件
是有意为之（转换后的 .docx 可能已被 reconvert 改名），只统计不拦截。

用法：
    python tools/purge_origin.py --roots "<RESOURCE_ROOT>\\就业资料" "<DOC_ROOT>\\待分类"
    python tools/purge_origin.py --roots ... --apply
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="删除已收入语料的原件")
    ap.add_argument("--roots", nargs="+", required=True, help="导入源目录白名单")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--log", default=str(config.BUILD_DIR / "origin_deleted.csv"))
    ap.add_argument("--apply", action="store_true", help="真删（否则只预览）")
    ap.add_argument("--rmdirs", action="store_true", help="顺手清掉删空后的空目录")
    args = ap.parse_args(argv)

    roots = [Path(os.path.abspath(r)) for r in args.roots]
    for r in roots:
        if not r.is_dir():
            print(f"[fatal] 不是目录：{r}", file=sys.stderr)
            return 2

    conn = db.connect(args.db, create=False)
    plan, kept = [], []
    no_repl = 0
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    h = hashing.sha256_file(p)[:16]
                except OSError as ex:
                    kept.append((str(p), f"读失败:{type(ex).__name__}"))
                    continue
                rows = conn.execute(
                    "SELECT doc_hash, status, src_path, file_name, note FROM source_documents "
                    "WHERE doc_hash=?",
                    (h,),
                ).fetchall()
                if not rows:
                    kept.append((str(p), "未收入"))
                    continue
                # 同一 hash 可能有多行（隔离副本 / 99 区待删副本）；只有出现"已入库"行才算收入
                ok_rows = [r for r in rows if r["status"] in OK_STATUS]
                if not ok_rows:
                    kept.append((str(p), "只有隔离副本:" + "/".join(sorted({r["status"] for r in rows}))))
                    continue
                keep = None
                for r in ok_rows:
                    cand = Path(r["src_path"])
                    try:
                        cand.resolve().relative_to(config.CORPUS_ROOT.resolve())
                    except ValueError:
                        continue
                    if cand.exists():
                        keep = cand
                        break
                if keep is None:
                    kept.append((str(p), "保留副本不在语料根内或不存在"))
                    continue
                plan.append((p, h, keep, p.stat().st_size))
                # 免检项：superseded 但 note 里没有接替去向（转换/退役口径没写清）→ 只统计提示
                if any(r["status"] == "superseded" and "converted_to:" not in (r["note"] or "")
                       for r in ok_rows):
                    no_repl += 1

    print(json.dumps({
        "roots": [str(r) for r in roots],
        "planned_delete": len(plan),
        "planned_bytes": sum(x[3] for x in plan),
        "kept": len(kept),
        "kept_reasons": {rr: sum(1 for _p, r2 in kept if r2 == rr) for _p, rr in kept},
        "superseded_no_replacement": no_repl,
        "dry_run": not args.apply,
    }, ensure_ascii=False, indent=2))
    for p, _h, keep, _s in plan[:6]:
        print(f"   删除 {p}  ←保留 {keep}")
    if not args.apply:
        conn.close()
        return 0

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    new_log = not log.exists()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    deleted = 0
    with log.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(["deleted_at", "origin_path", "size_bytes", "doc_hash", "kept_copy"])
        for p, h, keep, size in plan:
            try:
                p.unlink()
            except OSError as ex:
                print(f"[warn] 删除失败 {p}: {ex}", file=sys.stderr)
                continue
            deleted += 1
            w.writerow([now, str(p), size, h, str(keep)])

    if args.rmdirs:
        removed = 0
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                if dirpath == str(root):
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        removed += 1
                except OSError:
                    pass
        print(f"[rmdir] 清掉空目录 {removed} 个")

    conn.close()
    print(f"[done] 已删 {deleted}/{len(plan)} 件，明细 → {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
