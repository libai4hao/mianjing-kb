# -*- coding: utf-8 -*-
"""CLI stdio 编码兜底：Windows 重定向输出时不能因中文 JSON 崩掉。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from mianjing_kb.kb import stdio

REPO = Path(__file__).resolve().parents[1]


class _FakeStream:
    def __init__(self, encoding: str):
        self.encoding = encoding
        self.kw: dict | None = None

    def reconfigure(self, **kw):
        self.kw = kw


def test_configure_utf8_stdio_rewrites_cp1252(monkeypatch):
    out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    stdio.configure_utf8_stdio()

    assert out.kw == {"encoding": "utf-8", "errors": "replace"}
    assert err.kw == {"encoding": "utf-8", "errors": "replace"}


def test_configure_utf8_stdio_keeps_utf8_stream(monkeypatch):
    out, err = _FakeStream("utf-8"), _FakeStream("utf-8")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    stdio.configure_utf8_stdio()

    assert out.kw is None and err.kw is None


def test_cli_help_survives_cp1252_stdout():
    """子进程强制 cp1252：入口自己切 UTF-8 后中文帮助必须正常输出，而不是直接崩溃。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONUTF8"] = "0"
    p = subprocess.run(
        [sys.executable, "-m", "mianjing_kb.cli", "help"],
        cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert p.returncode == 0, p.stderr
    assert "命令：" in p.stdout


def test_module_entrypoint_survives_cp1252_stdout():
    """`python -m mianjing_kb.classify` 直接入口也必须走同一条 stdio 兜底。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONUTF8"] = "0"
    p = subprocess.run(
        [sys.executable, "-m", "mianjing_kb.classify", "--help"],
        cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert p.returncode == 0, p.stderr
    assert "classify" in p.stdout
