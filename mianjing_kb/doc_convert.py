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
    python -m mianjing_kb.doc_convert                 # 只看有多少待转
    python -m mianjing_kb.doc_convert --apply         # 真转 + 落库标记
    python -m mianjing_kb.doc_convert --apply --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, events, hashing, paths, proc, status, wlock  # noqa: E402

WD_FORMAT_XML_DOCUMENT = 16   # 2010+ 的 wdFormatDocumentDefault；2007 只认 12，见 convert()
WD_FORMAT_XML_DOCUMENT_2007 = 12
DOC_TIMEOUT = 180             # 单文件硬超时（秒）：Word COM 无法被打断，只能杀进程（T16）
REPO_ROOT = Path(__file__).resolve().parents[1]


def iter_doc(root: Path):
    reject = set()
    try:
        import yaml
        reject = set(yaml.safe_load((config.META_DIR / "folder_rules.yaml").read_text(encoding="utf-8"))
                     .get("reject_patterns", []))
    except Exception as ex:                                   # noqa: BLE001
        print(f"[warn] 拒收规则读取失败，本次不做拒收过滤：{ex}", file=sys.stderr)
    for dirpath, dirnames, filenames in os.walk(root):
        # 不跟随 junction/软链目录：os.walk 会顺着它们走出语料根
        dirnames[:] = [d for d in dirnames if not paths.is_link(Path(dirpath) / d)]
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
            p = Path(dirpath) / fn
            if paths.is_link(p) or not paths.resolves_inside(root, p):
                continue                                # 链接文件/越界文件不转
            yield p


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


def _cleanup_tmp(src: Path) -> None:
    """子进程被杀时，它的临时 docx 会留在源目录；父进程负责收尸。"""
    for p in src.parent.glob(f".{src.stem}.tmp-*.docx"):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _job_paths() -> tuple[Path, Path]:
    """每次转换独占的结果文件 / Word PID 文件（不进源目录，避免污染语料）。"""
    base = Path(tempfile.gettempdir()) / f"mianjing-docjob-{uuid.uuid4().hex}"
    return base.with_suffix(".json"), base.with_suffix(".wordpid")


def _worker_argv(src: Path, force: bool, result: Path, pidfile: Path) -> list[str]:
    """子进程命令行。单测会替换这个函数来注入"永不返回"的假 worker。"""
    argv = [sys.executable, "-m", "mianjing_kb.doc_convert",
            "--one", str(src), "--result", str(result), "--pidfile", str(pidfile)]
    if force:
        argv.append("--force")
    return argv


def _word_pid(word) -> int:
    """Word 的 PID（用于子进程被杀后兜底清理孤儿 WINWORD）。拿不到就返回 0。"""
    try:
        import win32process
        hwnd = int(word.Hwnd or 0)
        if hwnd:
            return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:                                    # noqa: BLE001
        return 0
    return 0


def _one(src: Path, force: bool, result: Path, pidfile: Path) -> int:
    """子进程入口：只转一个文件，把结论写进 result JSON。

    父进程靠"结果文件是否完整"区分失败与超时：超时被杀时文件不会存在。
    这里**不碰数据库、不拿写锁**——父进程已经持有 kb 锁，子进程再拿会自我死锁。
    """
    payload = {"how": "failed:unknown"}
    word = None
    try:
        import win32com.client as win32
    except ImportError:
        payload = {"how": "failed:no_pywin32"}
    else:
        try:
            word = win32.DispatchEx("Word.Application")   # 独立私有实例
            word.Visible = False
            word.DisplayAlerts = 0
            word.AutomationSecurity = 3                   # 禁宏
            pid = _word_pid(word)
            if pid:
                try:
                    pidfile.write_text(str(pid), encoding="utf-8")
                except OSError:
                    pass
            ok, how = convert(word, src, force)
            payload = {"how": how, "ok": bool(ok)}
        except Exception as ex:                           # noqa: BLE001
            payload = {"how": f"failed:{type(ex).__name__}: {str(ex)[:120]}"}
        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:                        # noqa: BLE001
                    pass
    try:
        result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return 3
    try:
        pidfile.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


def _convert_one_isolated(src: Path, force: bool = False, timeout: int = DOC_TIMEOUT
                          ) -> tuple[bool, str]:
    """一个文件一个子进程：超时杀树 → 记 timeout → 继续下一件（T16）。"""
    result, pidfile = _job_paths()
    argv = _worker_argv(src, force, result, pidfile)
    code, _out, err, timed_out = proc.run_with_timeout(argv, timeout, cwd=REPO_ROOT)
    try:
        if timed_out:
            # 先杀子进程树（Word 是它的子进程），再按子进程写下的 PID 兜底
            try:
                pid = int(pidfile.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            proc.kill_pid(pid)
            _cleanup_tmp(src)
            return False, "timeout"
        if code != 0 or not result.exists():
            _cleanup_tmp(src)
            why = proc.tail(err)
            return False, f"failed:worker_exit_{code}" + (f": {why}" if why else "")
        payload = json.loads(result.read_text(encoding="utf-8"))
        how = str(payload.get("how") or "failed:no_result")
        if how not in ("converted", "exists"):
            _cleanup_tmp(src)
        return how in ("converted", "exists"), how
    finally:
        for p in (result, pidfile):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _convert_batch(files, force: bool = False, timeout: int = DOC_TIMEOUT) -> list[dict]:
    """逐件隔离转换。任何一件超时/失败都不影响后续文件。"""
    results = []
    for src in files:
        _ok, how = _convert_one_isolated(src, force, timeout)
        results.append({"doc": str(src), "how": how})
        if how not in ("converted", "exists"):
            print(f"[warn] {src.name}: {how}", file=sys.stderr)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=".doc → .docx 并标记 superseded")
    ap.add_argument("--root", default=str(config.CORPUS_ROOT))
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--apply", action="store_true", help="真转（否则只预览）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="已有 .docx 不合格（非 zip）时强制重转")
    ap.add_argument("--timeout", type=int, default=DOC_TIMEOUT,
                    help=f"单文件硬超时秒数（默认 {DOC_TIMEOUT}）；超时杀进程并记 timeout")
    # 内部子进程参数（由 _worker_argv 调用，不对外承诺）
    ap.add_argument("--one", metavar="DOC", help=argparse.SUPPRESS)
    ap.add_argument("--result", metavar="JSON", help=argparse.SUPPRESS)
    ap.add_argument("--pidfile", metavar="FILE", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.one:                     # 子进程：单文件，不碰库、不拿锁
        return _one(Path(args.one), args.force,
                    Path(args.result), Path(args.pidfile or (args.result or "") + ".pid"))

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
    # T16：一个文件一个子进程。Word COM 不可被打断，超时只能杀进程树；
    # 任何一件卡死都不再阻塞整批（失败件记 how=timeout 并继续）。
    results = _convert_batch(files, args.force, args.timeout)
    stats = {"converted": 0, "exists": 0, "failed": 0, "timeout": 0}
    for r in results:
        how = r["how"]
        if how in ("converted", "exists"):
            stats[how] += 1
        elif how == "timeout":
            stats["timeout"] += 1
        else:
            stats["failed"] += 1

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
            conn.commit()          # 逐件提交：后面某件失败不连累前面已落盘的 docx 丢了账
            superseded += 1
    finally:
        conn.close()

    print(json.dumps({"stats": stats, "superseded_rows": superseded, "root": str(args.root)},
                     ensure_ascii=False, indent=2))
    print("[next] 接着跑：python -m mianjing_kb.intake && python -m mianjing_kb.classify")
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())

