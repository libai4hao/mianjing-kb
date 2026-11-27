# -*- coding: utf-8 -*-
"""标注工作簿解析预算回归（审计候选 #77）。"""
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "gen_answer_baseline", REPO / "scripts" / "gen_answer_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_answer_baseline"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_workbook(path, rows=3):
    wb = Workbook()
    ws = wb.active
    ws.title = "母版"
    gab_headers = _load().HEADERS
    ws.append(gab_headers)
    ws.append([])
    ws.append([])
    for i in range(rows):
        ws.append([f"s{i}", i, "h", "a/b.md"] + [None] * (len(gab_headers) - 4))
    wb.save(path)


def test_normal_workbook_loads(tmp_path, monkeypatch):
    gab = _load()
    p = tmp_path / "ok.xlsx"
    _mk_workbook(p, rows=5)
    monkeypatch.setattr(gab, "XLSX", p)
    rows = gab.load_annotations()
    assert len(rows) == 5 and rows[0]["sample_id"] == "s0"


def test_oversized_archive_rejected_before_open(tmp_path, monkeypatch):
    gab = _load()
    p = tmp_path / "big.xlsx"
    _mk_workbook(p)
    # 把压缩包撑过 64MiB 预算（追加不可压缩随机块）
    import os
    with open(p, "ab") as f:
        f.write(os.urandom(65 * 1024 * 1024))
    monkeypatch.setattr(gab, "XLSX", p)
    with pytest.raises(ValueError, match="过大"):
        gab.load_annotations()


def test_zip_entry_count_rejected(tmp_path, monkeypatch):
    gab = _load()
    p = tmp_path / "many.xlsx"
    with zipfile.ZipFile(p, "w") as zf:
        for i in range(5001):
            zf.writestr(f"m{i}.xml", "x")
    monkeypatch.setattr(gab, "XLSX", p)
    with pytest.raises(ValueError, match="条目过多"):
        gab.load_annotations()


def test_declared_row_count_rejected(tmp_path, monkeypatch):
    gab = _load()
    p = tmp_path / "rows.xlsx"
    _mk_workbook(p, rows=2)
    # 篡改 sheet dimension 声明超大 max_row（openpyxl read_only 按声明迭代）
    import re
    with zipfile.ZipFile(p) as zf:
        names = zf.namelist()
        data = {n: zf.read(n) for n in names}
    sheet = next(n for n in names if n.startswith("xl/worksheets/sheet"))
    data[sheet] = re.sub(rb'<dimension ref="[^"]*"',
                         b'<dimension ref="A1:AI999999"', data[sheet])
    with zipfile.ZipFile(p, "w") as zf:
        for n, b in data.items():
            zf.writestr(n, b)
    monkeypatch.setattr(gab, "XLSX", p)
    with pytest.raises(ValueError, match="行列超限"):
        gab.load_annotations()
