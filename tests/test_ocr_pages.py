# -*- coding: utf-8 -*-
"""ocr_pages 引擎崩溃重启（T10）：进程级断言——旧引擎子进程必须被 wait 回收。

用真实 subprocess 当假引擎（不启动 PaddleOCR），断言：
  1) 第一件触发 RuntimeError → run_batch 重启一次并跑完；
  2) 重启前的 pid 已死、重启后的 pid 在退出时也死（不是"对象置 None"的假回收）；
  3) ExitStack 收尾会再次调用旧引擎的 __exit__（必须幂等，不能抛）。
"""
import json
import os
import subprocess
import sys

import pymupdf
import pytest

from mianjing_kb import ocr_pages as op


class FakeEngine:
    """持有一个真实子进程的假引擎；第一次 ocr 抛 RuntimeError 模拟崩溃（只崩一次）。"""

    instances: list["FakeEngine"] = []
    crash_pending = True

    def __init__(self, *a, **kw):
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.pid = self.proc.pid
        self.exited = 0
        FakeEngine.instances.append(self)

    def __enter__(self):
        return self

    def ocr(self, image_path):
        if FakeEngine.crash_pending:
            FakeEngine.crash_pending = False
            raise RuntimeError("OCR 引擎进程已退出")
        return {"code": 100, "text": "识别文本", "score": 0.91, "lines": 1}

    def __exit__(self, *exc):
        self.exited += 1
        if self.proc is None:            # ExitStack 会对已手工退出的旧引擎再调一次
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None


def _alive(pid: int) -> bool:
    """pid 是否还活着（优先 psutil，退回 os.kill(pid,0)）。"""
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.fixture()
def fake_world(tmp_path, monkeypatch):
    FakeEngine.instances = []
    FakeEngine.crash_pending = True
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    pdf = corpus / "a.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()
    monkeypatch.setattr(op.config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(op, "PaddleOCRJson", FakeEngine)

    def fake_pages(pdf_path, pages, engine, dpi=200, tmp_dir=None):
        res = engine.ocr("page.png")     # 不走渲染；首次抛错触发重启
        return [{"page": 1, "text": res["text"], "score": res["score"],
                 "lines": res["lines"], "seconds": 0.1}]

    monkeypatch.setattr(op, "ocr_pdf_pages", fake_pages)
    return tmp_path, pdf


def test_batch_rejects_path_traversal(fake_world):
    tmp_path, _pdf = fake_world
    stats = op.run_batch(["../outside.pdf"], tmp_path / "ocr_text", tmp_path / "ocr.jsonl",
                         dpi=72, show=0, every=1)
    assert stats["ok"] == 0 and stats["fail"] == 1
    rows = [json.loads(x) for x in (tmp_path / "ocr.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["ok"] is False and "unsafe_path" in rows[-1]["reason"]
    assert not (tmp_path / "ocr_text" / "outside.pdf.txt").exists()


def test_crashed_engine_restarts_and_both_processes_are_waited(fake_world):
    tmp_path, _pdf = fake_world
    stats = op.run_batch(["a.pdf"], tmp_path / "ocr_text", tmp_path / "ocr.jsonl",
                         dpi=72, show=0, every=1)

    assert stats["ok"] == 1 and stats["fail"] == 0, (stats, len(FakeEngine.instances))
    assert len(FakeEngine.instances) == 2, "崩溃后应重启一次（且只重启一次）"

    old, new = FakeEngine.instances
    # 进程级断言：pid 已死，而不是只把对象引用置 None
    assert old.exited >= 1 and old.proc is None and not _alive(old.pid)
    assert new.exited >= 1 and new.proc is None and not _alive(new.pid)

    assert (tmp_path / "ocr_text" / "a.pdf.txt").exists()
    rows = [json.loads(x) for x in (tmp_path / "ocr.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["ok"] is True


def test_nonempty_legacy_ocr_text_is_not_resumed(fake_world):
    tmp_path, _pdf = fake_world
    out_root = tmp_path / "ocr_text"
    target = op.text_path_for("a.pdf", out_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("===== page 1 =====\ntruncated", encoding="utf-8")
    assert not op.is_complete_text("a.pdf", _pdf, out_root)
    stats = op.run_batch(["a.pdf"], out_root, tmp_path / "ocr.jsonl", dpi=72,
                         show=0, every=1)
    assert stats["ok"] == 1 and stats["resumed"] == 0
    assert op.is_complete_text("a.pdf", _pdf, out_root)


def test_ocr_sidecar_detects_output_tampering(fake_world):
    tmp_path, pdf = fake_world
    out_root = tmp_path / "ocr_text"
    pages = [{"page": 1, "text": "完整文本", "score": 0.9, "lines": 1, "seconds": 0.1}]
    saved = op.save_text("a.pdf", pages, out_root, source_pdf=pdf, total_pages=1)
    assert op.is_complete_text("a.pdf", pdf, out_root)
    saved.write_text("截断", encoding="utf-8")
    assert not op.is_complete_text("a.pdf", pdf, out_root)


def test_partial_page_export_is_not_marked_complete(fake_world):
    tmp_path, pdf = fake_world
    out_root = tmp_path / "ocr_text"
    pages = [{"page": 1, "text": "仅部分页", "score": 0.9, "lines": 1, "seconds": 0.1}]
    op.save_text("a.pdf", pages, out_root, source_pdf=pdf, total_pages=2)
    assert not op.is_complete_text("a.pdf", pdf, out_root)


def test_atomic_write_failure_preserves_previous_target(tmp_path, monkeypatch):
    from mianjing_kb.kb import atomic
    target = tmp_path / "result.txt"
    target.write_text("old-complete", encoding="utf-8")

    def fail_replace(_src, _dst):
        raise OSError("simulated power-loss before replace")

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic.atomic_write_text(target, "new-partial", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "old-complete"
    assert not list(tmp_path.glob(".result.txt.*.tmp"))


def test_duplicate_page_records_are_not_complete(fake_world):
    tmp_path, pdf = fake_world
    out_root = tmp_path / "ocr_text"
    page = {"page": 1, "text": "page", "score": 0.9, "lines": 1, "seconds": 0.1}
    op.save_text("a.pdf", [page, page], out_root, source_pdf=pdf, total_pages=1)
    assert not op.is_complete_text("a.pdf", pdf, out_root)


def test_pdf_change_during_ocr_is_not_marked_complete(fake_world, monkeypatch):
    tmp_path, pdf = fake_world
    original = op.ocr_pdf_pages

    def mutate_after_ocr(pdf_path, pages, engine, dpi):
        rows = original(pdf_path, pages, engine, dpi)
        pdf_path.write_bytes(pdf_path.read_bytes() + b"changed")
        return rows

    monkeypatch.setattr(op, "ocr_pdf_pages", mutate_after_ocr)
    out_root = tmp_path / "ocr_text"
    stats = op.run_batch(["a.pdf"], out_root, tmp_path / "ocr.jsonl", dpi=72,
                         show=0, every=1)
    assert stats["fail"] == 1 and stats["ok"] == 0
    assert not op.is_complete_text("a.pdf", pdf, out_root)
