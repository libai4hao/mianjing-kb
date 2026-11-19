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
from mianjing_kb.kb import config  # noqa: E402

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
    """把单页渲染成 PNG（1-based 页号）。"""
    import pymupdf
    png_path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(str(pdf_path)) as doc:
        if not 1 <= pno <= doc.page_count:
            raise ValueError(f"页号超界: {pno}/{doc.page_count}")
        pix = doc[pno - 1].get_pixmap(dpi=dpi)
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
    return out_root / (rel.replace("\\", "/") + ".txt")


def save_text(rel: str, pages: list[dict], out_root: Path) -> Path:
    """全文落盘：带页标记，便于下游回溯页码。"""
    tp = text_path_for(rel, out_root)
    tp.parent.mkdir(parents=True, exist_ok=True)
    parts = [PAGE_MARK.format(n=p["page"]) + p["text"] for p in pages]
    tp.write_text("".join(parts), encoding="utf-8")
    return tp


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
            pdf = config.CORPUS_ROOT / rel
            tp = text_path_for(rel, out_root)
            if tp.exists() and tp.stat().st_size > 0:
                stats["resumed"] += 1
                continue
            if not pdf.exists():
                stats["fail"] += 1
                _log(log_path, {"rel": rel, "ok": False, "reason": "file_missing"})
                continue
            import pymupdf
            with pymupdf.open(str(pdf)) as doc:
                pages = list(range(1, doc.page_count + 1))
            total_pages_est += len(pages)
            attempt = 0
            while True:
                try:
                    results = ocr_pdf_pages(pdf, pages, engine, dpi)
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
            save_text(rel, results, out_root)
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
                pdf = config.CORPUS_ROOT / rel
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
        saved = save_text(rel, results, out_root)
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
