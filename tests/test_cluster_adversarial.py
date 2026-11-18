# -*- coding: utf-8 -*-
"""99 区自动删重闸门的对抗性测试：**题干相同、答案不同** 的文件绝不能判成 strong。

覆盖三分支：
- strong：题干同、答案也同（真重复）→ 可删
- manual：题干同、答案不同 → 降级人工，附 answer_conflicts 证据
- none：题干不同/无题 → 保留
"""
import json
from pathlib import Path

import pytest
from mianjing_kb import cluster as cl  # noqa: E402

Q = "HashMap 在 JDK8 里是怎么实现的"
ANS_SAME = ("数组加链表加红黑树，扩容时按两倍容量迁移，链表长度超过八转红黑树，"
            "负载因子零点七五，并发场景用 ConcurrentHashMap。")
ANS_DIFF = ("Redis 的字典实现是渐进式 rehash，两个哈希表轮转迁移，"
            "扩容时不是一次性搬完，而是每次操作搬一个桶，避免长时间阻塞。")
PAD = "这一题的解析写得比较长，用来撑过无文本过滤的八十字符阈值，内容只是占位。"


def _doc(q: str, ans: str) -> str:
    return f"## 1. {q}\n\n答：{ans}{PAD}\n"


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(cl.config, "CORPUS_ROOT", tmp_path)
    monkeypatch.setattr(cl.config, "BUILD_DIR", build)   # 漏 patch 会写进生产 build
    monkeypatch.setattr(cl, "POOL_TOPS", ["pool"])
    monkeypatch.setattr(cl, "DIR99", "99d")
    monkeypatch.setattr(cl, "DIR20", "md20")
    for d in ("pool", "99d", "md20"):
        (tmp_path / d).mkdir()
    (tmp_path / "pool" / "库内.md").write_text(_doc(Q, ANS_SAME), encoding="utf-8")
    (tmp_path / "99d" / "同答重复件.md").write_text(_doc(Q, ANS_SAME), encoding="utf-8")
    (tmp_path / "99d" / "同题异答.md").write_text(_doc(Q, ANS_DIFF), encoding="utf-8")
    (tmp_path / "99d" / "无关件.md").write_text(
        _doc("Kafka 如何保证消息不重复消费", ANS_DIFF), encoding="utf-8")
    (tmp_path / "99d" / "无题讲义.md").write_text(PAD * 2, encoding="utf-8")
    return {"root": tmp_path, "build": build}


def _run(corpus, tmp_path):
    out = tmp_path / "res.json"
    assert cl.main(["--no-cache", "--out", str(out), "--sample", "0"]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _verdicts(res):
    return {Path(v["rel"]).name: v for v in res["verdict_detail"]}


def test_same_question_same_answer_is_strong(corpus, tmp_path):
    res = _run(corpus, tmp_path)
    v = _verdicts(res)
    assert v["同答重复件.md"]["verdict"] == "strong"
    assert v["同答重复件.md"]["answer_conflicts"] == []


def test_same_question_different_answer_downgraded_to_manual(corpus, tmp_path):
    res = _run(corpus, tmp_path)
    v = _verdicts(res)
    assert v["同题异答.md"]["verdict"] == "manual"      # 绝不自动删
    assert v["同题异答.md"]["answer_conflicts"]         # 证据留在结果里
    assert len(res["answer_conflicts"]) == 1
    assert "answer_conflicts" in res["stats"]


def test_unrelated_or_no_question_is_none(corpus, tmp_path):
    res = _run(corpus, tmp_path)
    v = _verdicts(res)
    assert v["无关件.md"]["verdict"] == "none"
    assert v["无题讲义.md"]["verdict"] == "none"


def test_conflict_does_not_cancel_legit_dedup(corpus, tmp_path):
    """同题异答降级只作用于那一件，同题的"同答重复件"仍可判 strong。"""
    res = _run(corpus, tmp_path)
    v = _verdicts(res)
    assert v["同答重复件.md"]["verdict"] == "strong"
    assert v["同题异答.md"]["verdict"] == "manual"
    assert res["verdict99"] == {"strong": 1, "manual": 1, "none": 2}
