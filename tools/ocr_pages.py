# -*- coding: utf-8 -*-
"""ocr_pages：PaddleOCR-json 页级 OCR（本地引擎，零 API 成本）。

引擎协议（实测）：stdin 每行一个 JSON {"image_path": "..."}；stdout 返回 JSON，code=100 表示成功。
约束：
  - 进程 cwd 必须是引擎目录；
  - 启动后需等 "OCR init completed."（实测初始化 0.76 s）；
  - 图片路径必须是 ASCII，含中文会被引擎拒；
  - 实测速度 0.72–0.80 s/页，score 多在 0.92 以上。

用法：
    python tools/ocr_pages.py --smoke                          # 只测引擎能否起来
    python tools/ocr_pages.py --pdf x.pdf --pages 1-3          # 渲染 + OCR 前 3 页
"""
from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config  # noqa: E402


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


def render_pages(pdf_path: Path, pages: list[int], out_dir: Path, dpi: int = config.OCR_DPI) -> list[Path]:
    """把指定页渲染成 ASCII 命名的 PNG（1-based 页号）。"""
    import pymupdf
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = []
    with pymupdf.open(str(pdf_path)) as doc:
        for pno in pages:
            if not 1 <= pno <= doc.page_count:
                continue
            pix = doc[pno - 1].get_pixmap(dpi=dpi)
            target = out_dir / f"page_{pno:04d}.png"
            pix.save(str(target))
            produced.append(target)
    return produced


def parse_range(text: str, total: int) -> list[int]:
    """'1-3,7' → [1,2,3,7]"""
    out = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return [p for p in out if 1 <= p <= total]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="页级 OCR（本地 PaddleOCR-json）")
    ap.add_argument("--smoke", action="store_true", help="只测引擎启动")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--pages", default="1-2", help="页范围，如 1-3,7")
    ap.add_argument("--dpi", type=int, default=config.OCR_DPI)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--show", type=int, default=60, help="每页打印前 N 个字符")
    args = ap.parse_args(argv)

    if args.smoke:
        with PaddleOCRJson() as engine:
            print(json.dumps({"engine": str(engine.exe), "init_seconds": engine.init_seconds}, ensure_ascii=False))
        return 0

    if not args.pdf:
        print("[fatal] 需要 --pdf 或 --smoke", file=sys.stderr)
        return 2
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[fatal] PDF 不存在: {pdf}", file=sys.stderr)
        return 2

    tmp = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="kbocr_"))
    if not str(tmp).isascii():
        tmp = Path(tempfile.mkdtemp(prefix="kbocr_"))     # 兜底：换 ASCII 目录
    import pymupdf
    with pymupdf.open(str(pdf)) as doc:
        pages = parse_range(args.pages, doc.page_count)
    images = render_pages(pdf, pages, tmp, args.dpi)

    rows = []
    t0 = time.perf_counter()
    with PaddleOCRJson() as engine:
        for pno, img in zip(pages, images):
            t1 = time.perf_counter()
            res = engine.ocr(img)
            rows.append({"page": pno, "score": res["score"], "lines": res["lines"],
                         "chars": len(res["text"]), "seconds": round(time.perf_counter() - t1, 2),
                         "preview": res["text"][: args.show]})
    print(json.dumps({
        "pdf": str(pdf),
        "pages": len(rows),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "avg_sec_per_page": round((time.perf_counter() - t0) / max(1, len(rows)), 2),
        "rows": rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
