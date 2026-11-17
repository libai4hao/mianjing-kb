# -*- coding: utf-8 -*-
"""dispatch（P2-② 分流）路由决策回归测试：decide 纯函数。"""
from tools.dispatch import decide


def _row(rel="12-综合题库/x.pdf", state="fail"):
    return {"rel": rel, "state": state}


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
