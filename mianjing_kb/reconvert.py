# -*- coding: utf-8 -*-
"""P2：同名覆盖重转——convert_log 里被"后转覆盖先转"顶掉的 26 条 + 指定补转件。

- 输出名带 doc_hash 后缀（sha256[:8]），杜绝再次同名覆盖
- 复用存量转换器 21-面经数据库\\tools\\convert_one.py（仅 fallback）（pymupdf4llm 主路 + 图片抽取）
- 源件已不存在/隔离的跳过并记因；每件追加 build/reconvert.jsonl，另存带时间戳的
  reconvert_log-*.json（reconvert_log.json 保留为最新副本）；失败数 >0 时退出码 1

用法：
    python -m mianjing_kb.reconvert --dry-run     # 只算清单不落盘
    python -m mianjing_kb.reconvert               # 实际重转
    python -m mianjing_kb.reconvert --extra "31-收件箱/..."   # 追加补转件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mianjing_kb"))

from mianjing_kb.kb import config, db, hashing, status, wlock  # noqa: E402

# 审核修复（2026-11-18）：原 insert(0) 会把语料侧 tools 顶到最前，遮蔽本仓库同名模块
# （同进程后续 from mianjing_kb.build_db import build 会解析到语料侧旧模块）；改为 append 放到末尾。
# 公开仓脱敏（2026-11-19）：语料侧 tools 路径不再硬编码，由 config.CORPUS_ROOT 推导；
# 路径不存在时模块仍可导入（换机器/未设 KB_CORPUS_ROOT 不至于连 import 都炸），调用时才报错。
# T17（2026-11-19 二审）：仓内 `kb/convert_one.py` 是主路，语料侧只是 fallback——
# 换机器不配语料侧 tools 也能跑转换；日志里记来源与 sha256，输出可回归。
def _load_convert_one():
    try:
        from mianjing_kb.kb import convert_one as repo_convert_one   # 仓内固定版（vendored）
        return repo_convert_one
    except ModuleNotFoundError as ex:
        if ex.name not in ("mianjing_kb.kb.convert_one", "mianjing_kb.kb"):
            raise                    # 仓内版本自身依赖缺失，不能被误报成"没配语料根"
    tools_dir = config.CORPUS_ROOT / "21-面经数据库" / "tools"
    if tools_dir.is_dir() and str(tools_dir) not in sys.path:
        sys.path.append(str(tools_dir))
    import convert_one  # noqa: F401  语料侧存量转换器（仅 fallback）
    return convert_one


def _converter_source(cv) -> str:
    """转换器来源：repo（仓内）/ corpus（语料侧 fallback）/ unknown。"""
    f = getattr(cv, "__file__", None)
    if not f:
        return "unknown"
    try:
        p = Path(f).resolve()
        p.relative_to((REPO / "mianjing_kb").resolve())
        return "repo"
    except ValueError:
        return "corpus"


convert_one = None


def _require_convert_one():
    """延迟加载：convert_one 自身依赖缺失（如 pymupdf4llm）不能被误报成"没配语料根"。"""
    global convert_one
    if convert_one is not None:
        return convert_one
    try:
        convert_one = _load_convert_one()
    except ModuleNotFoundError as ex:
        if ex.name in ("convert_one", "mianjing_kb.kb.convert_one", "mianjing_kb.kb"):
            raise RuntimeError(
                "仓库内 mianjing_kb/kb/convert_one.py 不可用，且语料侧也找不到 convert_one："
                "请检查仓库完整性，或设置 KB_CORPUS_ROOT 指向语料根（其中应含 "
                "21-面经数据库/tools/convert_one.py 作为 fallback）") from ex
        raise RuntimeError(f"convert_one 的依赖缺失：{ex.name}（请先安装该依赖）") from ex
    return convert_one


def _read_md(path: Path) -> str:
    """md 源解码：先 utf-8-sig，再 gb18030；替换符过多直接失败，不静默污染文本。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        bad = text.count("\ufffd")
        if bad > max(3, len(text) // 1000):
            raise ValueError(f"{path.name}: {enc} 解码后含 {bad} 个替换符")
        return text
    raise ValueError(f"{path.name}: 既不是 UTF-8 也不是 GB18030")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


BUILD = Path(config.BUILD_DIR)             # P1-3 修复（2026-11-17 审核）：路径统一走 config
MD_ROOT = Path(config.QUESTION_MD_DIR)     # KB_* 环境变量可覆盖，换机器不再失效
CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经", "个人面经": "个人面经", "专题笔记": "专题笔记"}



def losers_from_log() -> list[dict]:
    log = json.loads((BUILD / "convert_log.json").read_text(encoding="utf-8"))
    ok = [r for r in log if r.get("ok")]
    groups: dict[str, list] = defaultdict(list)
    for r in ok:
        exp = f"{r['category']}/{r['topic']}/{Path(r['rel']).stem}.md"
        groups[exp].append(r)
    losers = []
    for rs in groups.values():
        losers.extend(rs[:-1])  # 最后一个胜出，其余被覆盖
    return losers


def sha16(p: Path) -> str:
    """复用 kb.hashing.doc_hash，避免文件名哈希与库内 doc_hash 分叉。"""
    return hashing.doc_hash(p)


def convert_one_entry(rel: str, category: str, topic: str, dry_run: bool) -> dict:
    src = config.CORPUS_ROOT / rel
    out = {"rel": rel, "category": category, "topic": topic}
    if not src.exists():
        out.update(ok=False, err="源件不存在（已删/隔离）")
        return out
    cv = _require_convert_one()
    doc_hash = sha16(src)
    out["doc_hash"] = doc_hash
    stem = cv.safe_name(Path(rel).stem)
    out_dir = MD_ROOT / CAT_MAP.get(category, category) / (cv.safe_name(topic) or "综合")
    md_path = out_dir / f"{stem}_{doc_hash[:8]}.md"
    out["md"] = str(md_path.relative_to(MD_ROOT).as_posix())
    if dry_run:
        out.update(ok=None, note="dry-run")
        return out
    try:
        ext = src.suffix.lower()
        if ext == ".pdf":
            md, pages, kept, skipped = cv.pdf_to_md(
                str(src), str(out_dir / "images"), stem + f"_{doc_hash[:8]}", "pymupdf4llm")
        elif ext == ".docx":
            md = cv.docx_to_md(str(src))
            pages, kept, skipped = 1, 0, 0
        elif ext == ".md":
            # md 源的"转换"就是拷贝（原转换器不支持 .md，这 2 条当时也是直接落 md）
            md = _read_md(src)
            pages, kept, skipped = 1, 0, 0
        else:
            out.update(ok=False, err=f"暂不支持 {ext}")
            return out
        ratio = cv.cjk_ratio(md)
        if ratio < 0.05:
            out.update(ok=False, err=f"低CJK({ratio:.1%})")
            return out
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(md_path, md)
        out.update(ok=True, chars=len(md), pages=pages, cjk=round(ratio, 3),
                   imgs_kept=kept, imgs_skipped=skipped)
    except Exception as ex:  # noqa: BLE001
        out.update(ok=False, err=f"{type(ex).__name__}: {str(ex)[:200]}")
    return out


def _extra_job(conn, rel: str) -> dict | None:
    """--extra 必须按"目录 + 文件名"精确匹配；0 条或多条都报错，不猜。"""
    posix = rel.replace("\\", "/")
    p = Path(posix)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(Path(config.CORPUS_ROOT).resolve())
            posix = p.as_posix()
        except ValueError:
            print(f"[error] extra 不在语料根内: {rel}", file=sys.stderr)
            return None
    dir_path = p.parent.as_posix() if str(p.parent) not in (".", "") else ""
    name = p.name
    rows = conn.execute(
        "SELECT category, topic, dir_path, status FROM source_documents "
        "WHERE dir_path=? AND file_name=?",
        (dir_path, name),
    ).fetchall()
    if len(rows) != 1:
        print(f"[error] extra 匹配到 {len(rows)} 条记录（要求唯一）: {rel}", file=sys.stderr)
        return None
    row = rows[0]
    if row["status"] != status.CLASSIFIED:
        print(f"[warn] extra 记录不是 classified（{row['status']}）: {rel}", file=sys.stderr)
        return None
    return {"rel": f"{row['dir_path']}/{name}" if row["dir_path"] else name,
            "category": row["category"], "topic": row["topic"] or "综合", "_extra": True}


def _converter_meta(cv) -> dict:
    path = getattr(cv, "__file__", None)
    sha = hashing.sha256_file(path) if path and Path(path).is_file() else None
    try:
        from importlib.metadata import version
        ver = version("pymupdf4llm")
    except Exception:  # noqa: BLE001
        ver = None
    return {"convert_one": path, "convert_one_sha256": sha, "pymupdf4llm": ver,
            "convert_one_source": _converter_source(cv)}


def _run(args) -> int:
    try:
        jobs = losers_from_log()
    except FileNotFoundError:
        print(f"[fatal] 缺少转换日志：{BUILD / 'convert_log.json'}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as ex:
        print(f"[fatal] 转换日志不可读：{ex}", file=sys.stderr)
        return 2
    conn = db.connect(config.DB_PATH, create=False)
    try:
        for rel in args.extra:
            job = _extra_job(conn, rel)
            if job:
                jobs.append(job)
    finally:
        conn.close()

    cv = _require_convert_one() if jobs else None
    meta = _converter_meta(cv) if cv else {}
    started = time.perf_counter()
    results = []
    log_dir = BUILD
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl = log_dir / "reconvert.jsonl"
    for job in jobs:
        r = convert_one_entry(job["rel"], job["category"], job["topic"], args.dry_run)
        results.append(r)
        if not args.dry_run:
            with jsonl.open("a", encoding="utf-8") as fh:   # 每件追加：崩溃也能断点排查
                fh.write(json.dumps({**r, "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")},
                                    ensure_ascii=False) + "\n")
    elapsed = time.perf_counter() - started
    if not args.dry_run:
        payload = {"meta": meta, "results": results}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_log = log_dir / f"reconvert_log-{stamp}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        _write_atomic(run_log, text)
        _write_atomic(log_dir / "reconvert_log.json", text)   # 兼容旧文档口径
    ok_n = sum(1 for r in results if r["ok"] is True)
    skip_n = sum(1 for r in results if r["ok"] is False and r.get("err") == "源件不存在（已删/隔离）")
    fail_n = sum(1 for r in results if r["ok"] is False and r.get("err") != "源件不存在（已删/隔离）")
    print(json.dumps({"jobs": len(results), "ok": ok_n, "skipped_src_gone": skip_n,
                      "failed": fail_n, "dry_run": args.dry_run, "meta": meta,
                      "elapsed_sec": round(elapsed, 1),
                      "failures": [r for r in results if r["ok"] is False][:30]},
                     ensure_ascii=False, indent=1))
    return 0 if fail_n == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同名覆盖重转（输出名带 doc_hash 后缀）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--extra", nargs="*", default=[], help="追加补转的语料树相对路径")
    args = ap.parse_args(argv)

    if args.dry_run:
        return _run(args)
    try:
        with wlock.write_lock("kb", tool="reconvert",
                              build_dir=Path(config.DB_PATH).parent):
            return _run(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另一个写库工具在跑；等它结束再重试"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
