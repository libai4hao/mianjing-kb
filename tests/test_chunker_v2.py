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


def test_v2_bold_and_problem_n_signals():
    """讲解型 md 三形态（2026-11-17 增强，解锁 432 件 chunker_no_split 债务）：
    ① **粗体问句** 整行 → 块起点（含无问号但问词在前的变体）
    ② 问题N：正文 → 题号块（面向字节/连环炮系列主流形态，不依赖空行前置）
    ③ 问题N 纯标记行 → 武装紧随的独立问句行成为块起点
    ④ 非问句粗体 / 配图标题 不产题
    样本取自真实语料（面向字节_Dubbo / RabbitMQ / SpringBoot 系列）。"""
    md = """**Dubbo 支持哪些协议，每种协议的应用场景，优缺点？**

- **dubbo：** 单一长连接和 NIO 异步通讯，适合大并发小数据量的服务调用。

问题一：RabbitMQ 中的 broker 是指什么？cluster 又是指什么？

答：broker 是指一个或多个 erlang node 的逻辑分组。

问题二：什么是元数据？元数据分为哪些类型？

答：元数据主要分为 Queue 元数据。

问题三

什么是 Spring Boot？

多年来 spring 变得越来越复杂，需要很多努力配置。

**说说 Spring AOP**

AOP 是面向切面编程，解耦业务与横切关注点。

**注意：**

这里是非问句粗体强调，不应切题。

## 配图（2张）

![配图](./images/a.png)
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    # ① 粗体问句（行尾问号）
    assert any("Dubbo 支持哪些协议" in q for q in qs), qs
    # ② 问题N 带正文 → 题号块（问题二 无空行前置也切：作者显式标记不依赖排版）
    assert any("broker 是指什么" in q for q in qs), qs
    assert any("元数据分为哪些类型" in q for q in qs), qs
    # ③ 问题N 纯标记 → 下一行独立问句成为块起点
    assert any("Spring Boot" in q and "什么是" in q for q in qs), qs
    # ①' 粗体无问号但问词在前 8 字
    assert any("Spring AOP" in q for q in qs), qs
    # ④ 非问句粗体不切；配图标题不产题
    assert not any(q.strip() == "注意：" or "注意" == q.strip() for q in qs), qs
    assert not any("配图" in q for q in qs), qs
    # conf：问句/题号均 high；qtype 正常分类
    for it in v2["items"]:
        assert it["conf"] == "high", it
    # 答案归属：Dubbo 题的答案含 dubbo 协议讲解
    dubbo = next(it for it in v2["items"] if "Dubbo" in it["q"])
    assert "NIO 异步通讯" in dubbo["a"]
    # span 回指
    for it in v2["items"]:
        s, e = it["span"]
        assert 0 <= s < e <= len(md)
        assert it["q"][:6].lstrip("*# ") in md[s:s + 200]


def test_v2_bold_in_answer_split():
    """答案内部的粗体问句（空行前置）由 _split_answers 后置通道接住。"""
    md = """1. 谈谈你对索引的理解？

索引是加速查询的数据结构。

**什么是最左前缀原则？**

联合索引按最左列匹配查询。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert any("最左前缀" in q for q in qs), qs
    assert any("索引的理解" in q for q in qs), qs


def test_v2_problem_n_qid_progression():
    """问题N 题号递进：问题一/问题二 连续切题，qid 语义正确。"""
    md = """问题一：什么是 JVM？

虚拟机，运行字节码。

问题二：什么是 JMM？

Java 内存模型。
"""
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 2
    assert "JVM" in v2["items"][0]["q"] and "JMM" in v2["items"][1]["q"]
    assert "字节码" in v2["items"][0]["a"]
