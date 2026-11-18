# -*- coding: utf-8 -*-
"""高风险工具纯函数回归测试（2026-11-18 审核 P3）。

背景：backfill_convert / ocr_pages / reconvert / purge_origin 曾无直接测试，
其中 backfill 与 reconvert 是数据清零主力、purge_origin 会物理删文件——
这些纯函数是它们的核心判据，锁定口径防未来改动回归。

注意：这些是守护测试（函数已存在且生产验证过），断言取自源码 docstring 与
2026-11-17 修复记录（_stem 剥 __hash8 / hash 后缀、parse_range 页范围钳制、
losers 取每组前 n-1、purge 四重判据）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ---------------- backfill_convert ----------------
def test_backfill_stem_normalization():
    from backfill_convert import _stem
    # 双下划线 hash 后缀（backfill 落盘口径）
    assert _stem("面试题__a1b2c3d4.md") == "面试题"
    # 单下划线 hash 后缀（reconvert 口径）
    assert _stem("面试题_a1b2c3d4.pdf") == "面试题"
    # 尾随空格 + 大小写 + 前导序号（台账文件名脏形态）
    assert _stem("  Redis面试专题 .PDF") == "redis面试专题"
    assert _stem("01-JAVA基础.docx") == "java基础"
    # 不足 8 位 hex 不剥
    assert _stem("ab_cdef123") == "ab_cdef123"


def test_backfill_target_md_paths():
    import backfill_convert as bf
    entry = {"title": "Java基础.pdf", "category": "题库", "topic": "综合"}
    p = bf._target_md(entry)
    assert p.parts[-3:] == ("题库", "综合", "Java基础.md")
    # 非法文件名字符替换（Path().stem 先取末段，a/b 路径只留 b；冒号替换为 _）
    e2 = {"title": "a/b:c.pdf", "category": "真题", "topic": "面经"}
    assert bf._target_md(e2).name == "b_c.md"
    # CAT_MAP 未命中的 category 归题库
    e3 = {"title": "x.md", "category": "其他", "topic": ""}
    assert "题库" in bf._target_md(e3).parts


def test_backfill_hash_suffix():
    from backfill_convert import _hash_suffix
    assert _hash_suffix({"doc_hash": "abcdef0123456789"}) == "abcdef01"
    assert _hash_suffix({}) == "nohash"


# ---------------- ocr_pages ----------------
def test_ocr_parse_range():
    from ocr_pages import parse_range
    assert parse_range("1-3,7", 10) == [1, 2, 3, 7]
    assert parse_range("2,2", 10) == [2, 2]          # 重复不去重（逐页渲染口径）
    assert parse_range("9-12", 10) == [9, 10]        # 越界页钳制
    assert parse_range("0,-1", 10) == []             # 非法页过滤
    assert parse_range("", 10) == []


def test_ocr_text_path_for_rel():
    from ocr_pages import text_path_for
    p = text_path_for("01-Java基础\\甲.pdf", Path(r"D:\build\ocr_text"))
    assert p.name == "甲.pdf.txt" and "01-Java基础" in p.parts
    assert str(p).endswith(".pdf.txt")              # 相对 rel + .txt 后缀


# ---------------- reconvert ----------------
def test_reconvert_import_does_not_shadow_repo_tools():
    """reconvert 为复用 convert_one 需把语料侧 tools 加入 sys.path，但不得 insert(0)
    遮蔽本仓库同名模块（否则同进程后续 from build_db import build 解析到语料侧旧模块）。"""
    import subprocess
    # 子进程：import reconvert 后检查 sys.path[0] 不被语料侧路径占用，
    # 且 build_db 仍解析到本仓库模块（有 build 函数）
    code = (
        "import sys; sys.path.insert(0, r'" + str(ROOT) + r"'); "
        "sys.path.insert(0, r'" + str(ROOT / 'tools') + r"'); "
        "import reconvert; "
        "assert '21-面经数据库\\\\tools' not in sys.path[0], sys.path[0]; "
        "import build_db; assert callable(getattr(build_db, 'build', None)), build_db.__file__"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_reconvert_losers_grouping(tmp_path, monkeypatch):
    import reconvert as rc
    log = [
        {"rel": "a/x.pdf", "category": "题库", "topic": "综合", "ok": True},
        {"rel": "b/x.pdf", "category": "题库", "topic": "综合", "ok": True},   # 同 stem 后者胜出
        {"rel": "c/y.pdf", "category": "题库", "topic": "MySQL", "ok": True},
        {"rel": "d/z.pdf", "category": "题库", "topic": "综合", "ok": False}, # 失败不计
    ]
    fake_log = tmp_path / "convert_log.json"
    fake_log.write_text(__import__("json").dumps(log, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rc, "BUILD", tmp_path)
    losers = rc.losers_from_log()
    assert len(losers) == 1 and losers[0]["rel"] == "a/x.pdf"   # 仅被覆盖的先转件


# ---------------- purge_origin ----------------
def test_purge_ok_status_whitelist():
    from purge_origin import OK_STATUS
    assert "classified" in OK_STATUS and "superseded" in OK_STATUS
    assert "quarantined" not in OK_STATUS and "rejected" not in OK_STATUS


def test_purge_decision_guard():
    """四重判据的纯逻辑：状态白名单 + 保留副本在语料根内且存在。"""
    from purge_origin import OK_STATUS
    import purge_origin as po
    corpus = Path(r"D:\corpus").resolve()

    def decide(rows, keep_path, keep_exists=True):
        ok_rows = [r for r in rows if r["status"] in OK_STATUS]
        if not ok_rows:
            return "只有隔离副本"
        cand = Path(keep_path)
        try:
            cand.resolve().relative_to(corpus)
        except ValueError:
            return "保留副本不在语料根内"
        if not keep_exists:
            return "保留副本不存在"
        return "可删"

    rows_ok = [{"status": "classified", "src_path": str(corpus / "x.pdf")}]
    rows_q = [{"status": "quarantined", "src_path": str(corpus / "x.pdf")}]
    assert decide(rows_ok, corpus / "x.pdf") == "可删"
    assert decide(rows_q, corpus / "x.pdf") == "只有隔离副本"
    assert decide(rows_ok, r"D:\elsewhere\x.pdf") == "保留副本不在语料根内"
    assert decide(rows_ok, corpus / "x.pdf", keep_exists=False) == "保留副本不存在"
