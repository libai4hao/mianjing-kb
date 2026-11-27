# -*- coding: utf-8 -*-
"""kb/csvsafe：低信任文本进表格软件前的公式中和（全仓唯一实现）。"""
import pytest

from mianjing_kb.kb import csvsafe


@pytest.mark.parametrize("raw", [
    "=1+1", "+1", "-1", "@SUM(A1)", " =cmd|' /C calc'!A1", "\t=cmd", "\n=cmd", "\r=cmd",
])
def test_formula_like_cells_get_leading_apostrophe(raw):
    assert csvsafe.neutralize(raw) == "'" + raw


@pytest.mark.parametrize("raw", [
    "什么是 JVM", "v2.0-JavaGuide", "a-b", "1. 请写出线程池参数", "", None,
])
def test_plain_cells_are_untouched(raw):
    assert csvsafe.neutralize(raw) == ("" if raw is None else raw)


def test_neutralize_row_maps_every_value():
    row = {"rel": "=cmd", "count": 3, "note": None}
    assert csvsafe.neutralize_row(row) == {"rel": "'=cmd", "count": "3", "note": ""}
