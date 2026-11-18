# -*- coding: utf-8 -*-
"""dispatch（P2-② 分流）路由决策回归测试：decide 纯函数 + 目录口径 + 选行。"""
import csv
import json
import sqlite3

from mianjing_kb import dispatch
from mianjing_kb.dispatch import decide, dir_of
from mianjing_kb.kb import config


def _row(rel="12-综合题库/x.pdf", state="fail"):
    return {"rel": rel, "state": state}


def test_dir_of_uses_ledger_posix_convention():
    """台账 dir_path 是 posix、根级为空串；不能用 Path.parent（Windows 返回反斜杠与 '.'）。"""
    assert dir_of("11-面经与真题/11.3-专题总结包/甲.pdf") == "11-面经与真题/11.3-专题总结包"
    assert dir_of("甲.pdf") == ""


def test_route_prefers_same_dir_row(tmp_path, monkeypatch):
    """同名件跨目录：必须取同目录的那一行，不能被全局优先级（superseded > classified）带偏。"""
    build = tmp_path / "build"
    build.mkdir()
    (build / "ocr_text").mkdir()
    dbp = build / "kb.sqlite"
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    conn.executescript((config.META_DIR / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    for h, dirp, status in (("aaa0000000000001", "甲目录", "classified"),
                            ("bbb0000000000002", "乙目录", "superseded")):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (h, h + "0" * 48, "同名.pdf", ".pdf", 1, f"{dirp}/同名.pdf", dirp, status, "t", "now", "now"))
    conn.commit()
    conn.close()

    report = build / "gate.json"
    report.write_text(json.dumps({"rows": [{"rel": "甲目录/同名.pdf", "state": "fail", "reason": "乱码"}]},
                                 ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    monkeypatch.setattr(dispatch, "BUILD", build)
    monkeypatch.setattr(dispatch, "OCR_TEXT", build / "ocr_text")

    out = build / "plan.csv"
    assert dispatch.main(["--report", str(report), "--out", str(out)]) == 0
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["status"] == "classified"
    assert rows[0]["route"] == "needs_ocr"     # 同目录行已入库且未 OCR


def test_superseded_wins():
    assert decide(_row(), "superseded", "converted_to:...", False) == "superseded"


def test_rejected_skips():
    assert decide(_row(), "rejected", "", False) == "skip"


def test_ocr_done():
    assert decide(_row(), "classified", "", True) == "ocr_done"


def test_supersede_suggest_for_old_javaguide():
    r = _row("12-综合题库/v2.0-JavaGuide面试突击版.pdf")
    assert decide(r, "classified", "", False) == "supersede_suggest"


def test_review_and_needs_ocr():
    assert decide(_row(state="review"), "classified", "", False) == "review"
    assert decide(_row(), "classified", "", False) == "needs_ocr"


def test_priority_ocr_before_suggest():
    # v2.0-JavaGuide 若 OCR 已跑，按 ocr_done（OCR 是既成事实，退役建议降级）
    r = _row("12-综合题库/v2.0-JavaGuide面试突击版.pdf")
    assert decide(r, "classified", "", True) == "ocr_done"
