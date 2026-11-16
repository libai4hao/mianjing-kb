# -*- coding: utf-8 -*-
"""文本质量门禁指标的单元测试：健康中文 vs 字体编码损坏的乱码。"""
from kb import textmetrics as tm

HEALTHY = ("Java 面试题：HashMap 的扩容机制？答：默认容量 16，负载因子 0.75，"
           "扩容时容量翻倍并重新分配桶；红黑树化阈值为 8，退化阈值为 6。"
           "并发场景下 ConcurrentHashMap 使用 CAS + synchronized 保证线程安全。")

# 取自真实语料里的坏字体提取结果
GARBLED = "5.2̵\nړ\u0af2ୗ᭗ᦔᶎᦶෆቘ ٣٤ ٽ ٿ"


def test_healthy_cjk_ratio():
    assert tm.cjk_ratio(HEALTHY) > 0.4
    assert tm.garbled_ratio(HEALTHY) == 0.0


def test_garbled_detected():
    assert tm.cjk_ratio(GARBLED) < 0.05
    assert tm.garbled_ratio(GARBLED) > 0.3
    assert tm.metrics(GARBLED)["visible_chars"] > 0


def test_verdict_thresholds():
    kw = dict(cjk_min=0.15, garbled_max=0.01, valid_min=0.90, min_visible=800)
    assert tm.verdict(HEALTHY, **kw) == "non_content"        # 字符量不足
    assert tm.verdict(GARBLED, **kw) == "garbled"
    long_healthy = HEALTHY * 30
    assert tm.verdict(long_healthy, **kw) == "pass"


def test_strip_markdown_noise():
    src = "# 标题\n![图](a.png)\n[链接](http://x)\n```java\nint a=1;\n```\n正文"
    out = tm.strip_markdown_noise(src)
    assert "a.png" not in out and "http://x" not in out
    assert "正文" in out
