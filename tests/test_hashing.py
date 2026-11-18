# -*- coding: utf-8 -*-
"""hashing：长度偏置、包含度判定、标点归一的口径回归。"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from kb import hashing  # noqa: E402

POOL = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严"

CODE_SNIPPETS = [
    "x = a + b; if x > 0: y = x * 2",
    "x = a - b; if x < 0: y = x / 2",
]


def _sample_text(n: int, seed: int = 0) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(POOL) for _ in range(n))


def test_bottom_k_underestimates_contained_document():
    """旧口径的已知偏置：B 完全包含于 A，similarity 仍只得低分（防止悄悄"修好"又没人知道）。"""
    a = _sample_text(18_890, seed=1)
    b = a[:2_000]
    score = hashing.similarity(hashing.char_fingerprint(a), hashing.char_fingerprint(b))
    assert 0.0 < score < 0.5, score


def test_threshold_containment_is_length_invariant():
    """阈值采样：长文档 vs 其 1/9 节选，包含度应≈1。"""
    a = _sample_text(18_890, seed=1)
    b = a[:2_000]
    score = hashing.threshold_containment(
        hashing.threshold_fingerprint(a), hashing.threshold_fingerprint(b))
    assert score is not None and score > 0.95, score


def test_threshold_containment_rejects_unrelated():
    a = hashing.threshold_fingerprint(_sample_text(5_000, seed=2))
    b = hashing.threshold_fingerprint(_sample_text(5_000, seed=3))
    score = hashing.threshold_containment(a, b)
    assert score is not None and score < 0.2, score


def test_threshold_returns_none_when_too_short():
    tiny = hashing.threshold_fingerprint("短文本")
    assert hashing.threshold_containment(tiny, tiny) is None


def test_normalize_keeps_code_symbols_when_asked():
    assert hashing.normalize_text("a+b") == hashing.normalize_text("a-b")
    assert hashing.normalize_text("a+b", strip_punct=False) != hashing.normalize_text(
        "a-b", strip_punct=False)


def test_char_fingerprint_strip_punct_flag_changes_result():
    assert hashing.char_fingerprint(CODE_SNIPPETS[0], strip_punct=True) == \
        hashing.char_fingerprint(CODE_SNIPPETS[1], strip_punct=True)
    assert hashing.char_fingerprint(CODE_SNIPPETS[0], strip_punct=False) != \
        hashing.char_fingerprint(CODE_SNIPPETS[1], strip_punct=False)


def test_pixel_fingerprint_survives_missing_file():
    assert hashing.pixel_fingerprint("不存在.pdf") is None
