# -*- coding: utf-8 -*-
"""切题诊断 CLI（docs/61 T42）：离线部分的自检。"""
from mianjing_kb import chunk_diag as D


def test_norm_key_strips_space_and_case():
    assert D.norm_key("  What  Is\tJVM？ ") == "whatisjvm？"


def test_audit_items_counts_weak_and_tail():
    items = [
        {"q": "什么是 JVM 的类加载？", "a": "分五步。", "conf": "high", "qtype": "八股"},
        {"q": "目录", "a": "", "conf": "low", "qtype": "八股"},
    ]
    rows, f = D.audit_items(items, {"什么是jvm的类加载？"})
    assert f["weak"] == 1 and f["a_empty"] == 1
    assert rows[0]["new_only"] is False and rows[1]["new_only"] is True


def test_self_check_passes():
    assert D.cmd_self_check(None) == 0


def test_cli_parses_subcommands():
    ap = D.build_parser()
    ns = ap.parse_args(["diag", "--kb", "k.db", "--corpus", "md", "--tag", "x"])
    assert ns.cmd == "diag" and ns.tag == "x" and callable(ns.func)
    ns = ap.parse_args(["ablate", "--kb", "k.db", "--corpus", "md", "--rules", "标题探针"])
    assert ns.rules == "标题探针"
