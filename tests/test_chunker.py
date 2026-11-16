# -*- coding: utf-8 -*-
"""chunker 回归测试：8 组 fixtures（chunk_*.txt + 期望 .json）。

判据（与 docs 一致）：
① qid 原文存在  ② 答案内编号列表不判新题  ③ 代码块/表格内不切
④ 目录行不产出题目  ⑤ 页眉页脚/水印剔除  ⑥ span 回溯且全文不丢字
⑦ 图片占位归属前一块  ⑧ >1200 token 按段落续切
"""
import json
from pathlib import Path

import pytest

from tools import chunker

FIX = Path(__file__).parent / "fixtures"
CASES = sorted(FIX.glob("chunk_*.txt"))


def _get(block, key):
    idx, field = key.split(".", 1)
    return block[int(idx)][field]


def _covered_whitespace_only(md_text, result):
    """blocks + leftovers 区间并集应覆盖全文（允许只漏纯空白）。"""
    spans = [tuple(b["source_span"]) for b in result["blocks"]]
    spans += [tuple(l["span"]) for l in result["leftovers"]]
    spans.sort()
    covered = 0
    gap = 0
    cur_end = 0
    for s, e in spans:
        if s > cur_end:
            gap_text = md_text[cur_end:s]
            if gap_text.strip():
                return False, f"未覆盖非空白区间 [{cur_end},{s}]: {gap_text[:30]!r}"
            gap += len(gap_text)
        if e > cur_end:
            covered += e - max(s, cur_end)
            cur_end = max(cur_end, e)
    if cur_end < len(md_text):
        if md_text[cur_end:].strip():
            return False, "文末存在未覆盖非空白内容"
    return True, ""


@pytest.mark.parametrize("txt", CASES, ids=[p.stem for p in CASES])
def test_chunker_fixture(txt):
    md_text = txt.read_text(encoding="utf-8")
    exp = json.loads((txt.with_suffix(".json")).read_text(encoding="utf-8"))
    result = chunker.chunk(md_text)
    blocks = result["blocks"]
    leftovers = result["leftovers"]

    # 块数 / qid / style（判据①：qid 必须原文存在——这里校验与期望一致）
    assert len(blocks) == exp["block_count"], \
        f"块数 {len(blocks)} != {exp['block_count']}: {[b['q_text'][:20] for b in blocks]}"
    if "qids" in exp:
        assert [b["qid"] for b in blocks] == exp["qids"]
    if "styles" in exp:
        assert [b["style"] for b in blocks] == exp["styles"]

    # leftovers 的 why 多重集合一致（判据④⑤：目录/页码/水印进 leftovers 不产出题目）
    if "leftover_whys" in exp:
        got = sorted(l["why"] for l in leftovers)
        want = sorted(exp["leftover_whys"])
        assert got == want, f"leftovers: {got} != {want}"

    for key, sub in exp.get("contains", {}).items():
        v = _get(blocks, key)
        assert sub in v, f"contains 失败 {key}: {sub!r} not in {v[:60]!r}"
    for key, sub in exp.get("not_contains", {}).items():
        v = _get(blocks, key)
        assert sub not in v, f"not_contains 失败 {key}: {sub!r} in {v[:60]!r}"

    # 判据⑦：icons
    for idx, cnt in exp.get("icons", {}).items():
        assert len(blocks[int(idx)]["icons"]) == cnt

    # 判据⑥：span 回溯 + 全文不丢字
    for b in blocks:
        s, e = b["source_span"]
        assert 0 <= s < e <= len(md_text)
        seg = md_text[s:e]
        if b["q_text"]:
            assert b["q_text"] in seg, f"q_text 不在 span 内: {b['q_text'][:30]!r}"
    if exp.get("coverage"):
        ok, why = _covered_whitespace_only(md_text, result)
        assert ok, why

    # 判据①补强：qid 必须出现在原文对应 span 里（题号原文存在，绝不生成）。
    # 汉字题号（一、→1）按原汉字校验；"/续" 块 qid 继承自首块，跳过。
    han_of = {v: k for k, v in chunker._HAN.items()}
    for b in blocks:
        if b["qid"] is None or b["style"].endswith("/续"):
            continue
        seg = md_text[b["source_span"][0]:b["source_span"][0] + 200]
        assert str(b["qid"]) in seg or han_of.get(b["qid"], "") in seg, \
            f"qid {b['qid']} 未在原文 span 出现"


def test_chunker_is_pure_and_safe():
    """纯函数：同输入同输出；空串/纯页码不崩。"""
    assert chunker.chunk("") == {"blocks": [], "leftovers": []}
    r1 = chunker.chunk("## 1. A？\n\n答案\n")
    r2 = chunker.chunk("## 1. A？\n\n答案\n")
    assert r1 == r2
    assert r1["blocks"][0]["qid"] == 1
