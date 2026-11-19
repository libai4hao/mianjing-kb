# -*- coding: utf-8 -*-
"""T16 回归：Word 单文件转换的进程级超时（一个坏文件不能拖死整批）。

策略：不依赖本机是否装 Word——把 `_worker_argv` 换成可控的假 worker：
- 永不返回的 worker → 必须记 `timeout` 并继续下一件；
- 写结果文件的 worker → 正常读回 `converted`；
- 崩溃且无结果的 worker → 记 `failed:worker_exit_N`（与超时可区分）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from mianjing_kb import doc_convert as dc  # noqa: E402
from mianjing_kb.kb import proc           # noqa: E402


def _hang_argv(*_a, **_k):
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def test_run_with_timeout_kills_and_reports():
    t0 = time.time()
    code, out, err, timed = proc.run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=2)
    assert timed is True
    assert time.time() - t0 < 15, "超时后应立即杀进程，而不是等它自己结束"
    assert code not in (0, None) or code is None      # 被杀 → 非 0


def test_run_with_timeout_normal_exit():
    code, out, _err, timed = proc.run_with_timeout(
        [sys.executable, "-c", "print('ok')"], timeout=30)
    assert (code, timed) == (0, False)
    assert "ok" in out


def test_hanging_file_is_recorded_and_batch_continues(tmp_path, monkeypatch):
    a, b = tmp_path / "a.doc", tmp_path / "b.doc"
    a.write_bytes(b"old a")
    b.write_bytes(b"old b")
    monkeypatch.setattr(dc, "_worker_argv", _hang_argv)

    t0 = time.time()
    results = dc._convert_batch([a, b], timeout=2)
    elapsed = time.time() - t0

    assert [r["how"] for r in results] == ["timeout", "timeout"]
    assert elapsed < 20, f"两件各 2s 超时，不该用 {elapsed:.1f}s"


def test_result_file_is_read_back(tmp_path, monkeypatch):
    src = tmp_path / "x.doc"
    src.write_bytes(b"old")

    def fake(src_, force, result, pidfile):
        result.write_text(json.dumps({"how": "converted", "ok": True}), encoding="utf-8")
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(dc, "_worker_argv", fake)
    assert dc._convert_one_isolated(src, timeout=30) == (True, "converted")


def test_worker_crash_without_result_is_distinguishable(tmp_path, monkeypatch):
    src = tmp_path / "y.doc"
    src.write_bytes(b"old")

    def fake(src_, force, result, pidfile):
        return [sys.executable, "-c", "import sys; sys.exit(1)"]

    monkeypatch.setattr(dc, "_worker_argv", fake)
    ok, how = dc._convert_one_isolated(src, timeout=30)
    assert ok is False and how.startswith("failed:worker_exit_1")


def test_timeout_cleans_worker_tmp_docx(tmp_path, monkeypatch):
    """子进程被杀后，它没来得及删的临时 docx 必须由父进程收拾。"""
    src = tmp_path / "z.doc"
    src.write_bytes(b"old")
    stale = tmp_path / ".z.tmp-999.docx"
    stale.write_bytes(b"half")
    monkeypatch.setattr(dc, "_worker_argv", _hang_argv)
    assert dc._convert_one_isolated(src, timeout=2) == (False, "timeout")
    assert not stale.exists()


def _fake_win32():
    import types
    mod = types.ModuleType("win32com.client")
    mod.DispatchEx = lambda _n: (_ for _ in ()).throw(RuntimeError("no word in CI"))
    pkg = types.ModuleType("win32com")
    pkg.client = mod
    return {"win32com": pkg, "win32com.client": mod}


def test_one_mode_writes_result_even_on_import_failure(tmp_path, monkeypatch):
    """子进程入口：依赖缺失/Word 起不来也要落结果文件，父进程才能区分失败与超时。"""
    src = tmp_path / "w.doc"
    src.write_bytes(b"old")
    res = tmp_path / "res.json"
    for name, m in _fake_win32().items():
        monkeypatch.setitem(sys.modules, name, m)
    assert dc.main(["--one", str(src), "--result", str(res), "--pidfile", str(res) + ".pid"]) == 0
    payload = json.loads(res.read_text(encoding="utf-8"))
    assert payload["how"].startswith("failed:")
