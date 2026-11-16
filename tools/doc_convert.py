# -*- coding: utf-8 -*-
"""doc_convert：把老 `.doc` 转成同名 `.docx`，并把原 `.doc` 记录标 superseded。

为什么需要：语料里有 `.doc`（Word 97-2003），解析链只认 pdf/docx/md。专项转换脚本以前
只在语料内 `scripts\\补录-doc转docx.ps1` 里，且 PowerShell + 中文路径容易踩坑，这里做成
工具链一环（Word COM，`Office 12.0` 实测可用）。

流程（`--apply` 才动盘）：
  ① 扫语料里的 `.doc`（排除 `99-重复文件待删`、`00-索引`、`20-题库MD`、`21-面经数据库`）
  ② 逐个另存为同目录同名 `.docx`（已存在则跳过）
  ③ 把库内该 `.doc` 的记录改成 `status='superseded'`、`note='converted_to:<docx>'`，
     这样它不再进可入库清单；新的 `.docx` 由 `intake` 正常登记成新源

用法：
    python tools/doc_convert.py                 # 只看有多少待转
    python tools/doc_convert.py --apply         # 真转 + 落库标记
    python tools/doc_convert.py --apply --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db  # noqa: E402

WD_FORMAT_XML_DOCUMENT = 16   # 2010+ 的 wdFormatDocumentDefault；2007 只认 12，见 convert()
WD_FORMAT_XML_DOCUMENT_2007 = 12


def iter_doc(root: Path):
    reject = set()
    try:
        import yaml
        reject = set(yaml.safe_load((config.META_DIR / "folder_rules.yaml").read_text(encoding="utf-8"))
                     .get("reject_patterns", []))
    except Exception:                                   # noqa: BLE001 —— 规则读不到就不做拒收过滤
        pass
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix() if dirpath != str(root) else ""
        if any(seg in config.EXCLUDE_DIR_NAMES for seg in Path(rel).parts if seg) or \
                any(rel == p or rel.startswith(p + "/") for p in config.EXCLUDE_PREFIXES) or \
                rel.startswith(config.QUARANTINE_PREFIX):
            dirnames[:] = []
            continue
        for fn in filenames:
            if not fn.lower().endswith(".doc"):
                continue
            full_rel = f"{rel}/{fn}" if rel else fn
            if reject and any(fnmatch(full_rel, pat) for pat in reject):
                continue                                # 拒收位（简历模板/学习路线/图解课件）不转
            yield Path(dirpath) / fn


def _is_zip(path: Path) -> bool:
    """真 docx 是 zip（PK\\x03\\x04）。踩过：Word 2007 上 SaveAs2 不存在，
    存出来是 OLE 容器顶着 .docx 后缀，解析链一律 BadZipFile。"""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def convert(word, src: Path, force: bool = False) -> tuple[bool, str]:
    dst = src.with_suffix(".docx")
    if dst.exists() and not force:
        return (_is_zip(dst), "exists") if _is_zip(dst) else (False, "exists_not_zip")
    if dst.exists() and force:
        try:
            dst.unlink()
        except OSError as ex:
            return False, f"unlink_failed:{ex}"
    doc = word.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
    try:
        # Word 2007 没有 SaveAs2；连着试两种写法，最后统一用魔数验收
        for fmt in (WD_FORMAT_XML_DOCUMENT_2007, WD_FORMAT_XML_DOCUMENT):
            try:
                doc.SaveAs2(str(dst), FileFormat=fmt)
            except Exception:                     # noqa: BLE001
                doc.SaveAs(str(dst), FileFormat=fmt)
            if _is_zip(dst):
                return True, "converted"
    finally:
        doc.Close(False)
    if dst.exists() and not _is_zip(dst):
        try:
            dst.unlink()                          # 假的 docx 不要留在磁盘上
        except OSError:
            pass
        return False, "not_zip"
    return bool(dst.exists()), "converted" if dst.exists() else "empty_output"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=".doc → .docx 并标记 superseded")
    ap.add_argument("--root", default=str(config.CORPUS_ROOT))
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--apply", action="store_true", help="真转（否则只预览）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="已有 .docx 不合格（非 zip）时强制重转")
    args = ap.parse_args(argv)

    files = sorted(iter_doc(Path(args.root)))
    if args.limit:
        files = files[: args.limit]
    print(f"[{'apply' if args.apply else 'dry'}] 待转 .doc：{len(files)} 件")
    if not args.apply:
        for f in files[:10]:
            print("   ", str(f).replace(str(args.root), "…"))
        if len(files) > 10:
            print(f"    … 其余 {len(files) - 10} 件")
        return 0

    if not files:
        return 0

    try:
        import win32com.client as win32
    except ImportError:
        print("[fatal] 需要 pywin32：pip install pywin32", file=sys.stderr)
        return 2
    word = win32.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0

    stats = {"converted": 0, "exists": 0, "failed": 0}
    results = []
    try:
        for src in files:
            try:
                ok, how = convert(word, src, args.force)
            except Exception as ex:                      # noqa: BLE001
                ok, how = False, f"{type(ex).__name__}: {ex}"[:120]
                word = win32.Dispatch("Word.Application")   # Word 崩了就重开一个
                word.Visible = False
                word.DisplayAlerts = 0
            stats[how if how in stats else "failed"] += 1
            results.append({"doc": str(src), "how": how})
            if how not in ("converted", "exists"):
                print(f"[warn] {src.name}: {how}", file=sys.stderr)
    finally:
        try:
            word.Quit()
        except Exception:                                # noqa: BLE001
            pass

    conn = db.connect(args.db, create=False)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    superseded = 0
    for r in results:
        src = Path(r["doc"])
        if not src.with_suffix(".docx").exists():
            continue
        rows = conn.execute(
            "SELECT doc_hash, status FROM source_documents WHERE file_name=? AND src_path=?",
            (src.name, str(src)),
        ).fetchall()
        for row in rows:
            if row["status"] in ("quarantined", "absent", "superseded"):
                continue
            conn.execute(
                "UPDATE source_documents SET status='superseded', note=?, last_seen_at=? WHERE doc_hash=?",
                (f"converted_to:{src.with_suffix('.docx').name}", now, row["doc_hash"]),
            )
            superseded += 1
    conn.commit()
    conn.close()

    print(json.dumps({"stats": stats, "superseded_rows": superseded, "root": str(args.root)},
                     ensure_ascii=False, indent=2))
    print("[next] 接着跑：python tools\\intake.py && python tools\\classify.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
