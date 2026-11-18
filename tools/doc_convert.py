# -*- coding: utf-8 -*-
"""doc_convert：把老 `.doc` 转成同名 `.docx`，并把原 `.doc` 记录标 superseded。

为什么需要：语料里有 `.doc`（Word 97-2003），解析链只认 pdf/docx/md。专项转换脚本以前
只在语料内 `scripts\\补录-doc转docx.ps1` 里，且 PowerShell + 中文路径容易踩坑，这里做成
工具链一环（Word COM，`Office 12.0` 实测可用）。

流程（`--apply` 才动盘）：
  ① 扫语料里的 `.doc`（排除 `99-重复文件待删`、`00-索引`、`20-题库MD`、`21-面经数据库`）
  ② 逐个另存为同目录同名 `.docx`：先写临时文件，zip 完整校验通过才原子改名；
     已存在合法 docx 一律不动（`--force` 也只处理不合格的同名 docx，且挪成 `.bad` 而非删除）
  ③ 只有 `how=='converted'` 才把库内该 `.doc` 记录改成 `status='superseded'`、
     `note='converted_to:<dir>/<name>#<docx_hash>'`（前置追加，保留原 note）；
     `exists` 无法证明产物来自当前源件，不标记；新的 `.docx` 由 `intake` 正常登记成新源

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
import zipfile
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db, events, hashing, status, wlock  # noqa: E402

WD_FORMAT_XML_DOCUMENT = 16   # 2010+ 的 wdFormatDocumentDefault；2007 只认 12，见 convert()
WD_FORMAT_XML_DOCUMENT_2007 = 12


def iter_doc(root: Path):
    reject = set()
    try:
        import yaml
        reject = set(yaml.safe_load((config.META_DIR / "folder_rules.yaml").read_text(encoding="utf-8"))
                     .get("reject_patterns", []))
    except Exception as ex:                                   # noqa: BLE001
        print(f"[warn] 拒收规则读取失败，本次不做拒收过滤：{ex}", file=sys.stderr)
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
    """粗判：真 docx 是 zip（PK\\x03\\x04）。只用于快速排除 OLE 伪装件。"""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _valid_docx(path: Path) -> bool:
    """完整校验：zip 可读完、CRC 全通过、且含 word/document.xml。

    只查 4 字节魔数会把截断/半截文件当成合格 docx（评审 2026-11-19 中风险）。
    """
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False
            return "word/document.xml" in z.namelist()
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def _bad_name(dst: Path) -> Path:
    """给不合格的同名 .docx 找一个不覆盖的 .bad 落点。"""
    bad = dst.with_name(dst.name + ".bad")
    i = 1
    while bad.exists():
        bad = dst.with_name(f"{dst.name}.bad{i}")
        i += 1
    return bad


def convert(word, src: Path, force: bool = False) -> tuple[bool, str]:
    dst = src.with_suffix(".docx")
    tmp = dst.with_name(f".{dst.stem}.tmp-{os.getpid()}.docx")
    if dst.exists():
        if _valid_docx(dst):
            # --force 的语义只是"已有 docx 不合格时重转"；合法 docx 绝不能删
            return True, "exists"
        if not force:
            return False, "exists_not_zip"
        try:
            dst.replace(_bad_name(dst))       # 不删，挪成 .bad 留证
        except OSError as ex:
            return False, f"quarantine_bad_failed:{ex}"

    doc = None
    try:
        doc = word.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False,
                                  PasswordDocument="__no_password__",
                                  WritePasswordDocument="__no_password__")
        for fmt in (WD_FORMAT_XML_DOCUMENT_2007, WD_FORMAT_XML_DOCUMENT):
            try:
                doc.SaveAs2(str(tmp), FileFormat=fmt)
            except Exception:                     # noqa: BLE001
                doc.SaveAs(str(tmp), FileFormat=fmt)
            if _valid_docx(tmp):
                os.replace(tmp, dst)              # 校验通过才原子更名
                return True, "converted"
        return False, "not_zip" if tmp.exists() else "empty_output"
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:                     # noqa: BLE001
                pass
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


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
        __import__("win32com.client")          # 只探测依赖，真正实例在 _apply 里创建
    except ImportError:
        print("[fatal] 需要 pywin32：pip install pywin32", file=sys.stderr)
        return 2
    # 本工具既是写盘（生成 .docx）也是写库（标 superseded），全程持写锁：
    # 与 intake / batch_label / review_cli 互斥，别让另一个写库工具读到半更新账本。
    try:
        with wlock.write_lock("kb", tool="doc_convert", build_dir=Path(args.db).parent):
            return _apply(files, args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另一个写库工具在跑；等它结束再重试"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def _apply(files, args) -> int:
    import win32com.client as win32

    word = win32.DispatchEx("Word.Application")   # 独立私有实例，不劫持用户正在用的 Word
    word.Visible = False
    word.DisplayAlerts = 0
    word.AutomationSecurity = 3                   # 3 = msoAutomationSecurityForceDisable，禁宏

    stats = {"converted": 0, "exists": 0, "failed": 0}
    results = []
    try:
        for src in files:
            try:
                how = convert(word, src, args.force)[1]
            except Exception as ex:                      # noqa: BLE001
                how = f"{type(ex).__name__}: {str(ex)[:120]}"
                try:
                    word.Quit()                          # 旧实例先收掉，避免 WINWORD 泄漏
                except Exception:                        # noqa: BLE001
                    pass
                word = win32.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                word.AutomationSecurity = 3
            stats[how if how in stats else "failed"] += 1
            results.append({"doc": str(src), "how": how})
            if how not in ("converted", "exists"):
                print(f"[warn] {src.name}: {how}", file=sys.stderr)
    finally:
        try:
            word.Quit()
        except Exception:                                # noqa: BLE001
            pass

    superseded = 0
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = db.connect(args.db, create=False)
    try:
        for r in results:
            # 只有"真的转换成功"才允许标记 superseded；
            # exists（同名 docx 已存在）无法证明产物来自当前 .doc，不能碰原记录。
            if r["how"] != "converted":
                continue
            src = Path(r["doc"])
            docx = src.with_suffix(".docx")
            if not _valid_docx(docx):
                print(f"[warn] {src.name}: 产物校验失败，未标记 superseded", file=sys.stderr)
                continue
            doc_hash = hashing.sha256_file(src)[:16]
            row = db.get_by_hash(conn, doc_hash)
            if row is None:
                print(f"[warn] {src.name}: 转换成功但数据库无对应源记录（先跑 intake）",
                      file=sys.stderr)
                continue
            if row["status"] in (status.QUARANTINED, status.ABSENT, status.SUPERSEDED):
                continue
            rel_docx = (f"{row['dir_path']}/{docx.name}" if row["dir_path"] else docx.name)
            target = {"dir": row["dir_path"] or "", "name": docx.name,
                      "hash": hashing.sha256_file(docx)[:16]}
            note = f"converted_to:{rel_docx}#{target['hash']}"
            conn.execute(
                f"UPDATE source_documents SET status='{status.SUPERSEDED}', last_seen_at=?, "
                "note=CASE WHEN note IS NULL OR note='' THEN ? ELSE ? || ' | ' || note END "
                "WHERE doc_hash=?",
                (now, note, note, doc_hash),
            )
            # 事件与 note 双写：G5 / purge 读事件，人工仍看 note（T12）
            events.append(conn, doc_hash, events.KIND_CONVERTED_TO, target, at=now)
            superseded += 1
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"stats": stats, "superseded_rows": superseded, "root": str(args.root)},
                     ensure_ascii=False, indent=2))
    print("[next] 接着跑：python tools\\intake.py && python tools\\classify.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

