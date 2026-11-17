# -*- coding: utf-8 -*-
"""chunk_v2 回归测试（切题器 v2 规范：放宽判题信号 + qtype/conf/probe/coverage）。

判据：
① 问句/考点信号：空行前置的独立问句行、'知识点：'行另起新题（v1 题号行为主）
② R2'：答案内部列举编号、句中问词不判新题
③ qtype：HR/算法/项目/场景/八股 关键词启发式
④ conf：题号/问句=high，标题/考点=low
⑤ probe：items 为空时必须非空（1–3 个候选）
⑥ coverage：items 覆盖/(全长-扣除) ≥ 0.90（正常题文 fixture）
⑦ 致谢/招聘广告短行 → leftovers(致谢/广告)，不产题
⑧ span 不越界不重叠，q 必须能在原文 span 首段回指
"""
from tools import chunker

DOC = """# 面试题整理

第 1 页

1. 请简单介绍一下自己？

我是xx，五年Java后端经验。

1、负责过订单系统
2、熟悉高并发场景

2. 什么是 Java 反射？

反射是在运行时获取类信息的机制。

具体代码如下：

```java
Class<?> c = Class.forName("A");
```

如何理解 JVM 的类加载过程？

类加载分为加载、验证、准备、解析、初始化五个阶段。

知识点：HashMap 的扩容机制

默认容量16，负载因子0.75，超过阈值时扩容为两倍。

感谢阅读，欢迎关注公众号

第 2 页
"""


def test_v2_signals_and_schema():
    v2 = chunker.chunk_v2(DOC)
    items = v2["items"]
    # 题号2题 + 问句1题 + 考点1题 = 4；题1 自我介绍即 HR 题
    assert [it["seq"] for it in items] == [1, 2, 3, 4]
    assert items[0]["qtype"] == "HR" and items[0]["conf"] == "high"
    assert "反射" in items[1]["q"] and items[1]["conf"] == "high"
    assert "类加载" in items[2]["q"] and items[2]["conf"] == "high"   # 问句信号
    assert "扩容机制" in items[3]["q"] and items[3]["conf"] == "low"  # 考点信号
    # ② R2'：题1 答案内 "1、2、" 列表不拆；列表内容仍在题1答案里
    assert "订单系统" in items[0]["a"] and "高并发" in items[0]["a"]
    # ⑦ 致谢/广告被扣除，不产题
    assert "公众号" not in "".join(it["a"] for it in items)
    # ⑥ coverage
    assert v2["coverage"] >= 0.90, v2["note"]
    # ⑧ span 回指：问句/考点题干必须出现在各自 span 开头附近
    doc = DOC
    for it in items:
        s, e = it["span"]
        assert 0 <= s < e <= len(doc)
        assert it["q"][:6].lstrip("# ") in doc[s:s + 200]


def test_v2_qtype():
    q = lambda t: chunker.classify_qtype(t)
    assert q("请做一下自我介绍？") == "HR"
    assert q("如何手写一个LRU？") == "算法"
    assert q("说说你项目里遇到的难点？") == "项目"
    assert q("如何设计一个秒杀系统？") == "场景"
    assert q("什么是Java反射？") == "八股"


def test_v2_probe_when_no_items():
    prose = """这是一份没有任何题号的说明文字。

系统整体分为三个模块，各自职责如下，需要注意的地方不多。

整体而言可以优化，比如如何减少内存占用？这在实践中很常见。
"""
    v2 = chunker.chunk_v2(prose)
    assert v2["items"] == []
    assert 1 <= len(v2["probe"]) <= 3
    assert all("why" in p and len(p["span"]) == 2 for p in v2["probe"])
    # probe span 指向含问词的行
    s, e = v2["probe"][-1]["span"]
    assert "内存" in prose[s:e]


def test_v2_r2_answer_numbers_not_split():
    md = """1. 谈谈你对AQS的理解？

AQS是队列同步器。要点：
1) state变量
2) CLH队列
3) 独占与共享模式

2. volatile 为什么能保证可见性？

基于内存屏障实现。
"""
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 2
    assert "CLH队列" in v2["items"][0]["a"]
    assert v2["coverage"] >= 0.95


def test_v2_no_split_on_inline_intro_word():
    """回归：行中"详细介绍"不触发问句信号（PS 行不切）。"""
    md = """1. 什么是Redis？

Redis是内存数据库。

PS：篇幅问题，我这并没有对分布式缓存选型做详细介绍和对比，感兴趣的话，可以自行研究一下。

2. Redis为什么这么快？

基于内存和IO多路复用。
"""
    v2 = chunker.chunk_v2(md)
    assert [it["seq"] for it in v2["items"]] == [1, 2]
    assert "详细介绍" in v2["items"][0]["a"]
    assert v2["coverage"] >= 0.95


def test_render_two_steps():
    out = chunker.render(DOC)
    assert out.startswith("第一步：题目清单")
    assert "第二步：" in out
    assert '"items"' in out and '"coverage"' in out
    # 清单行 ≤ 12 字概述（去掉序号前缀后）
    first = out.splitlines()[1]
    assert len(first.split(". ", 1)[1]) <= 12
