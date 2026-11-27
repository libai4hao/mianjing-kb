# -*- coding: utf-8 -*-
"""ocr_pages：PaddleOCR-json 页级 OCR（本地引擎，零 API 成本）。

引擎协议（实测）：stdin 每行一个 JSON {"image_path": "..."}；stdout 返回 JSON，code=100 表示成功。
约束：
  - 进程 cwd 必须是引擎目录；
  - 启动后需等 "OCR init completed."（实测初始化 0.8–1.0 s）；
  - 图片路径必须是 ASCII，含中文会被引擎拒（文本输出路径不受限）；
  - 实测速度 0.8–1.0 s/页，score 多在 0.90 以上。

用法：
    python -m mianjing_kb.ocr_pages --smoke                                   # 只测引擎能否起来
    python -m mianjing_kb.ocr_pages --pdf x.pdf --pages 1-3 --save-text      # 单件：渲染 + OCR + 全文落盘
    python -m mianjing_kb.ocr_pages --batch build/ocr_needed.json --apply    # 批量：清单驱动 + 断点续跑 + JSONL 留痕
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import atomic, config, paths  # noqa: E402

PAGE_MARK = "\n\n===== page {n} =====\n\n"


def _flatten_texts(node, out: list[str]) -> None:
    """从引擎返回的任意嵌套结构里把 text 收齐。"""
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        for v in node.values():
            _flatten_texts(v, out)
    elif isinstance(node, list):
        for v in node:
            _flatten_texts(v, out)


def _collect_scores(node, out: list[float]) -> None:
    if isinstance(node, dict):
        s = node.get("score")
        if isinstance(s, (int, float)):
            out.append(float(s))
        for v in node.values():
            _collect_scores(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_scores(v, out)


class PaddleOCRJson:
    """PaddleOCR-json 的常驻进程封装，一页一次请求，避免反复加载模型。"""

    def __init__(self, engine_dir: Path | None = None, exe: Path | None = None,
                 startup_timeout: float = 20.0, reply_timeout: float = 120.0):
        self.engine_dir = Path(engine_dir or config.OCR_ENGINE_DIR)
        self.exe = Path(exe or config.OCR_ENGINE_EXE)
        self.startup_timeout = startup_timeout
        self.reply_timeout = reply_timeout
        self.proc: subprocess.Popen | None = None
        self._q: "queue.Queue[str|None]" = queue.Queue()
        self.init_seconds: float | None = None

    def __enter__(self) -> "PaddleOCRJson":
        if not self.exe.exists():
            raise FileNotFoundError(f"OCR 引擎不存在: {self.exe}")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.engine_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = t0 + self.startup_timeout
        while time.perf_counter() < deadline:
            line = self._next_line(max(0.1, deadline - time.perf_counter()))
            if line is None:
                continue
            if "OCR init completed" in line:
                self.init_seconds = round(time.perf_counter() - t0, 2)
                return self
        self.__exit__(None, None, None)
        raise TimeoutError("引擎启动超时：未见 'OCR init completed.'")

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def _next_line(self, timeout: float) -> str | None:
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise RuntimeError("OCR 引擎进程已退出")
        return item.strip()

    def ocr(self, image_path: Path | str) -> dict:
        assert self.proc and self.proc.stdin
        path = Path(image_path)
        if not str(path).isascii():
            raise ValueError(f"图片路径必须是 ASCII（引擎限制）: {path}")
        self.proc.stdin.write(json.dumps({"image_path": str(path)}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.perf_counter() + self.reply_timeout
        while time.perf_counter() < deadline:
            line = self._next_line(max(0.1, deadline - time.perf_counter()))
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "code" in payload:
                if str(payload.get("code")) != "100":
                    raise RuntimeError(f"OCR 引擎返回失败码 {payload.get('code')}: "
                                       f"{str(payload.get('data'))[:160]}")
                texts, scores = [], []
                _flatten_texts(payload.get("data"), texts)
                _collect_scores(payload.get("data"), scores)
                return {
                    "code": payload.get("code"),
                    "text": "\n".join(texts),
                    "score": round(sum(scores) / len(scores), 4) if scores else None,
                    "lines": len(texts),
                }
        raise TimeoutError(f"OCR 无响应超过 {self.reply_timeout}s: {path.name}")

    def __exit__(self, *exc) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None


def render_page(pdf_path: Path, pno: int, png_path: Path, dpi: int = config.OCR_DPI) -> Path:
    """把单页渲染成 PNG（1-based 页号）。

    #63：渲染前按页盒几何核算像素预算——超大页盒先拒绝，不先分配再失败。
    """
    import pymupdf

    from mianjing_kb.kb import pdfbudget
    png_path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(str(pdf_path)) as doc:
        if not 1 <= pno <= doc.page_count:
            raise ValueError(f"页号超界: {pno}/{doc.page_count}")
        page = doc[pno - 1]
        pdfbudget.check_render_geometry(page.rect.width, page.rect.height, dpi)
        pix = page.get_pixmap(dpi=dpi)
        pix.save(str(png_path))
    return png_path


def render_pages(pdf_path: Path, pages: list[int], out_dir: Path, dpi: int = config.OCR_DPI) -> list[Path]:
    """把指定页渲染成 ASCII 命名的 PNG（兼容旧接口）。"""
    produced = []
    for pno in pages:
        produced.append(render_page(pdf_path, pno, out_dir / f"page_{pno:04d}.png", dpi))
    return produced


def parse_range(text: str, total: int) -> list[int]:
    """'1-3,7' → [1,2,3,7]。非法段（含负数/空段/非数字）跳过而非抛异常
    （2026-11-18 审核修复：原 `chunk.split('-',1)` 会把 '-1' 拆成空串致 ValueError）。"""
    out = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:                      # 首字符的 '-' 是负号，不作为范围分隔
            a_s, b_s = chunk.split("-", 1)
            try:
                out.extend(range(int(a_s), int(b_s) + 1))
            except ValueError:                    # 段内含非数字 → 跳过该段
                continue
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return [p for p in out if 1 <= p <= total]


def ocr_pdf_pages(pdf_path: Path, pages: list[int], engine: PaddleOCRJson,
                  dpi: int = config.OCR_DPI, tmp_dir: Path | None = None) -> list[dict]:
    """逐页 渲染→OCR→删图，返回 [{page,text,score,lines,seconds}]。图片不落盘累积。"""
    tmp = tmp_dir or Path(tempfile.mkdtemp(prefix="kbocr_"))
    tmp.mkdir(parents=True, exist_ok=True)
    png = tmp / "page_now.png"          # 复用同一文件名，天然 ASCII 且不累积
    out = []
    try:
        for pno in pages:
            t1 = time.perf_counter()
            render_page(pdf_path, pno, png, dpi)
            res = engine.ocr(png)
            out.append({"page": pno, "text": res["text"], "score": res["score"],
                        "lines": res["lines"], "seconds": round(time.perf_counter() - t1, 2)})
    finally:
        png.unlink(missing_ok=True)
        if tmp_dir is None:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def text_path_for(rel: str, out_root: Path) -> Path:
    """语料相对路径 → OCR 全文 txt 路径（build/ocr_text/<rel>.txt）。"""
    return paths.safe_join(out_root, rel.replace("\\", "/") + ".txt")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text_meta_path_for(rel: str, out_root: Path) -> Path:
    return paths.safe_join(out_root, rel.replace("\\", "/") + ".txt.meta.json")


def save_text(rel: str, pages: list[dict], out_root: Path, *,
              source_pdf: Path | None = None, total_pages: int | None = None,
              source_sha256: str | None = None) -> Path:
    """原子落盘 OCR 全文；sidecar 证明源 PDF 与完整页集，旧非空文件不再视为完成。"""
    tp = text_path_for(rel, out_root)
    parts = [PAGE_MARK.format(n=p["page"]) + p["text"] for p in pages]
    text = "".join(parts)
    atomic.atomic_write_text(tp, text, encoding="utf-8")
    if source_pdf is not None and total_pages is not None:
        page_numbers = sorted({int(p["page"]) for p in pages})
        all_pages = list(range(1, int(total_pages) + 1))
        meta = {
            "format_version": 1,
            "source_sha256": source_sha256 or _sha256_file(Path(source_pdf)),
            "total_pages": int(total_pages),
            "completed_pages": page_numbers,
            "complete": len(pages) == len(page_numbers) == int(total_pages)
                        and page_numbers == all_pages,
            "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chars": len(text),
        }
        atomic.atomic_write_text(text_meta_path_for(rel, out_root),
                                 json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return tp


def is_complete_text(rel: str, source_pdf: Path, out_root: Path) -> bool:
    """验证 OCR 文本属于当前 PDF 且覆盖所有页，且 sidecar 的输出 hash 匹配。"""
    try:
        tp = text_path_for(rel, out_root)
        meta_path = text_meta_path_for(rel, out_root)
        if not tp.is_file() or not meta_path.is_file():
            return False
        import pymupdf
        with pymupdf.open(str(source_pdf)) as doc:
            total_pages = doc.page_count
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta.get("format_version") != 1 or meta.get("complete") is not True
                or meta.get("total_pages") != total_pages
                or meta.get("completed_pages") != list(range(1, total_pages + 1))):
            return False
        if meta.get("source_sha256") != _sha256_file(source_pdf):
            return False
        return meta.get("output_sha256") == _sha256_file(tp)
    except Exception:  # noqa: BLE001 —— 完整性校验必须 fail closed（含 PDF parser 错误）
        return False


def load_batch(path: Path) -> list[str]:
    """清单文件：接受 ['rel', ...] 或 [{'rel': ...}, ...]。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items", [])
    rels = []
    for x in items:
        rel = x if isinstance(x, str) else x.get("rel")
        if rel:
            rels.append(rel)
    return rels


def run_batch(rels: list[str], out_root: Path, log_path: Path, dpi: int,
              show: int, every: int = 20) -> dict:
    """批量执行：跳过已有全文的件（续跑）；引擎崩溃自动重启一次。"""
    stats = {"ok": 0, "resumed": 0, "fail": 0, "pages": 0, "chars": 0}
    t_start = time.perf_counter()
    done = 0
    total_pages_est = 0
    with contextlib.ExitStack() as stack:
        # ExitStack 登记：崩溃重启后新引擎同样在退出时被 close——直接用 with 的话，
        # 块结束调的是"进入时那个对象"的 __exit__，重启出来的新进程会漏掉（2026-11-19 审核修复）
        engine = stack.enter_context(PaddleOCRJson())
        for rel in rels:
            try:
                pdf = paths.safe_join(config.CORPUS_ROOT, rel, must_exist=True)
                text_path_for(rel, out_root)
            except (paths.UnsafePath, OSError) as ex:
                stats["fail"] += 1
                _log(log_path, {"rel": rel, "ok": False, "reason": f"unsafe_path: {ex}"})
                continue
            if not pdf.exists():
                stats["fail"] += 1
                _log(log_path, {"rel": rel, "ok": False, "reason": "file_missing"})
                continue
            import pymupdf

            from mianjing_kb.kb import pdfbudget
            source_sha256 = _sha256_file(pdf)
            try:
                with pymupdf.open(str(pdf)) as doc:
                    pdfbudget.check_page_count(doc.page_count)   # #63：页数预算
                    pages = list(range(1, doc.page_count + 1))
                    page_count = doc.page_count
            except pdfbudget.ResourceBudgetError as ex:
                stats["fail"] += 1
                _log(log_path, {"rel": rel, "ok": False, "reason": f"budget: {ex}"})
                continue
            total_pages_est += len(pages)
            if is_complete_text(rel, pdf, out_root):
                stats["resumed"] += 1
                continue
            attempt = 0
            while True:
                try:
                    results = ocr_pdf_pages(pdf, pages, engine, dpi)
                    break
                except pdfbudget.ResourceBudgetError as ex:
                    # #63：超大页盒渲染预算超限——记失败跳过该件，不重试不崩溃
                    stats["fail"] += 1
                    _log(log_path, {"rel": rel, "ok": False, "reason": f"budget: {ex}"})
                    results = None
                    break
                except (RuntimeError, TimeoutError) as ex:
                    attempt += 1
                    if attempt > 1:
                        stats["fail"] += 1
                        _log(log_path, {"rel": rel, "ok": False, "reason": f"{type(ex).__name__}: {ex}"})
                        results = None
                        break
                    engine.__exit__(None, None, None)   # 引擎崩溃 → 重启一次
                    engine = stack.enter_context(PaddleOCRJson())
            if results is None:
                continue
            if _sha256_file(pdf) != source_sha256:
                stats["fail"] += 1
                _log(log_path, {"rel": rel, "ok": False,
                                "reason": "source_changed_during_ocr"})
                continue
            save_text(rel, results, out_root, source_pdf=pdf, total_pages=page_count,
                      source_sha256=source_sha256)
            chars = sum(len(p["text"]) for p in results)
            scores = [p["score"] for p in results if p["score"] is not None]
            stats["ok"] += 1
            stats["pages"] += len(results)
            stats["chars"] += chars
            _log(log_path, {"rel": rel, "ok": True, "pages": len(results), "chars": chars,
                            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
                            "sec": round(time.perf_counter() - t_start, 1)})
            done += 1
            if show and done % every == 0:
                print(f"[ocr] {done}/{len(rels)} 件, {stats['pages']} 页, "
                      f"{round(time.perf_counter() - t_start, 1)}s", flush=True)
    stats["elapsed_sec"] = round(time.perf_counter() - t_start, 1)
    return stats


def _log(log_path: Path, row: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), **row},
                            ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="页级 OCR（本地 PaddleOCR-json）")
    ap.add_argument("--smoke", action="store_true", help="只测引擎启动")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--pages", default=None, help="页范围，如 1-3,7；缺省=全部页")
    ap.add_argument("--dpi", type=int, default=config.OCR_DPI)
    ap.add_argument("--out-dir", default=None, help="渲染图临时目录（缺省自动建/自动清）")
    ap.add_argument("--show", type=int, default=60, help="每页打印前 N 个字符；0=静默")
    ap.add_argument("--batch", default=None, help="清单 JSON（['rel',...] 或 [{'rel':...}]）")
    ap.add_argument("--apply", action="store_true", help="批量真跑（否则只 dry-run 统计页数）")
    ap.add_argument("--out-root", default=str(config.BUILD_DIR / "ocr_text"),
                    help="OCR 全文落盘根目录（默认 build/ocr_text）")
    ap.add_argument("--every", type=int, default=20, help="批量进度打印间隔（件数）")
    args = ap.parse_args(argv)

    if args.smoke:
        with PaddleOCRJson() as engine:
            print(json.dumps({"engine": str(engine.exe), "init_seconds": engine.init_seconds}, ensure_ascii=False))
        return 0

    out_root = Path(args.out_root)

    if args.batch:
        rels = load_batch(Path(args.batch))
        if not args.apply:
            import pymupdf
            total = 0
            missing = []
            for rel in rels:
                try:
                    pdf = paths.safe_join(config.CORPUS_ROOT, rel, must_exist=True)
                except (paths.UnsafePath, OSError):
                    missing.append(rel)
                    continue
                if not pdf.exists():
                    missing.append(rel)
                    continue
                with pymupdf.open(str(pdf)) as doc:
                    total += doc.page_count
            print(json.dumps({"dry_run": True, "items": len(rels), "missing": missing,
                              "total_pages": total}, ensure_ascii=False, indent=2))
            return 0
        stats = run_batch(rels, out_root, config.BUILD_DIR / "ocr_results.jsonl", args.dpi, args.show, args.every)
        print(json.dumps({"batch": args.batch, **stats}, ensure_ascii=False, indent=2))
        return 0

    if not args.pdf:
        print("[fatal] 需要 --pdf / --batch 或 --smoke", file=sys.stderr)
        return 2
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[fatal] PDF 不存在: {pdf}", file=sys.stderr)
        return 2

    import pymupdf
    source_sha256 = _sha256_file(pdf)
    with pymupdf.open(str(pdf)) as doc:
        total = doc.page_count
    pages = parse_range(args.pages, total) if args.pages else list(range(1, total + 1))

    tmp = Path(args.out_dir) if args.out_dir else None
    if tmp is not None and not str(tmp).isascii():
        tmp = None                                        # 兜底：换 ASCII 临时目录
    t0 = time.perf_counter()
    with PaddleOCRJson() as engine:
        results = ocr_pdf_pages(pdf, pages, engine, args.dpi, tmp)
    rows = [{**{k: p[k] for k in ("page", "score", "lines", "seconds")},
             "chars": len(p["text"]), "preview": p["text"][: args.show]} for p in results]
    saved = None
    if args.pdf:
        try:
            rel = str(pdf.relative_to(config.CORPUS_ROOT))
        except ValueError:
            rel = pdf.name
        if _sha256_file(pdf) != source_sha256:
            raise RuntimeError("PDF 在 OCR 期间发生变化；拒绝将结果标记为完成")
        saved = save_text(rel, results, out_root, source_pdf=pdf, total_pages=total,
                          source_sha256=source_sha256)
    print(json.dumps({
        "pdf": str(pdf), "pages": len(rows), "elapsed_sec": round(time.perf_counter() - t0, 2),
        "avg_sec_per_page": round((time.perf_counter() - t0) / max(1, len(rows)), 2),
        "saved_text": str(saved) if saved else None, "rows": rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
