# -*- coding: utf-8 -*-
"""面向 cut1000-raw100-20261120 的文档结构/边界回归。"""
from pathlib import Path

from mianjing_kb import chunker

FIX = Path(__file__).parent / "fixtures"


def test_dev_ocr_numbered_code_recovers_three_real_prompts():
    """真实开发.doc：连续编号代码不能成为题目，三个自然语言任务不能被吞掉。"""
    md = (FIX / "dev_ocr_numbered_code.md").read_text(encoding="utf-8")
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]

    assert len(qs) == 3
    assert "比较两个字符串的不同" in qs[0]
    assert "给定一个 100 亿的整数文件" in qs[1]
    assert "通过随机函数生成随机数填充一个二维数组" in qs[2]
    # 代码材料仍留在答案中，没有被静默丢弃。
    assert "compare_strings" in v2["items"][0]["a"]
    assert "output_min_k_elements" in v2["items"][1]["a"]
    assert "int main" in v2["items"][2]["a"]
    # 第一段 convert_path_opt 代码没有明确父问题：保留为 leftovers，而非编造题目。
    v1 = chunker.chunk(md, split_long=False)
    assert any("convert_path_opt" in md[x["span"][0]:x["span"][1]] for x in v1["leftovers"])
    # S0684-S0733 的代码/注释碎片词组不得成为题干。
    forbidden = ("int path_len", "path_len = strlen", "fprintf", "while (index",
                 "返回 NULL", "left_child", "int range = 100000000")
    assert not any(any(x in q for x in forbidden) for q in qs)
    assert v2["coverage"] > 0.95


def test_toc_with_wrapped_entries_and_page_markers_is_not_emitted():
    md = """# 面试题汇总

目录

1. 什么是 MyBatis？........................................ 12
2. 如何理解缓存？............................................ 14

第1 页共2 页

3. 什么是事务？.............................................. 18

第2 页共2 页

## MyBatis 面试题

### 1. 什么是 MyBatis？
MyBatis 是持久层框架。

### 2. 如何理解缓存？
缓存用于减少重复查询。
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert qs == ["什么是 MyBatis？", "如何理解缓存？"]
    assert "目录" not in "\n".join(qs)
    assert v2["coverage"] > 0.90


def test_chapter_heading_resets_qid_and_closes_previous_answer():
    md = """# Java 面试

## JVM 篇

27、什么是 TLAB？
TLAB 是线程本地分配缓存。

## 对象模型篇

1、对象头具体包含哪些内容？
对象头包含 Mark Word 和类型指针。
"""
    blocks = chunker.chunk(md, split_long=False)["blocks"]
    assert [b["qid"] for b in blocks] == [27, 1]
    assert "对象头" not in blocks[0]["a_text"]
    assert "TLAB" not in blocks[1]["q_text"]
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 2
    assert "对象头" not in v2["items"][0]["a"]


def test_acid_and_framework_feature_lists_stay_in_parent_answer():
    md = """1. 请说明 ACID 的四个基本要素？
四个基本要素包括：

1. 原子性（Atomicity）
2. 一致性（Consistency）
3. 隔离性（Isolation）
4. 持久性（Durability）

2. 集合框架有哪些优点？
集合框架的部分优点如下：

(1) 降低开发成本。
(2) 提高代码质量。
(3) 降低维护成本。
(4) 复用性和可操作性。

3. 什么是泛型？
泛型允许在编译时检查类型。
"""
    v2 = chunker.chunk_v2(md)
    assert [x["q"] for x in v2["items"]] == [
        "请说明 ACID 的四个基本要素？", "集合框架有哪些优点？", "什么是泛型？"]
    assert "持久性" in v2["items"][0]["a"]
    assert "复用性和可操作性" in v2["items"][1]["a"]


def test_answer_boundary_on_numbered_markdown_headings_and_plain_question():
    md = """#### 9、一个线程如果出现了运行时异常会怎么样
如果异常没有捕获，线程会停止执行。

#### 10、Hashtable 能同时有两条线程获取数据吗？

#### 20、ReadWriteLock 是什么
读写锁允许并发读并保证写互斥。
"""
    v2 = chunker.chunk_v2(md)
    assert [x["q"] for x in v2["items"]] == [
        "一个线程如果出现了运行时异常会怎么样",
        "Hashtable 能同时有两条线程获取数据吗？",
        "ReadWriteLock 是什么",
    ]
    assert "ReadWriteLock" not in v2["items"][0]["a"]
    assert v2["items"][1]["a"].strip() == ""
    assert "读写锁允许" in v2["items"][2]["a"]


def test_numbered_markdown_topic_heading_with_explanation_is_low_conf_item():
    md = """## 11. Oracle JDK 和 OpenJDK 的对比
Oracle JDK 是 Oracle 发布的 JDK，OpenJDK 是开源参考实现。

## MyBatis 适用场合
用于数据持久层。
"""
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 1
    assert "Oracle JDK" in v2["items"][0]["q"]
    assert v2["items"][0]["conf"] == "high"  # 原文明确 Markdown 编号题头
    assert "持久层" in v2["items"][0]["a"]  # “适用场合”保留为答案小节，不另起题


def test_hierarchical_numbered_question_is_parsed_without_chapter_prefix():
    md = """## MongoDB

11.3.1 什么是复制？
复制是将数据同步到多个节点。
"""
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 1
    assert v2["items"][0]["q"] == "什么是复制？"
    assert "多个节点" in v2["items"][0]["a"]


def test_comma_numbered_and_linked_questions_keep_the_question_text():
    md = """#### 62，说说PreparedStatement和Statement的区别
两者接口不同，PreparedStatement 支持预编译。

[4. 如何规避 JavaScript 多人开发函数重名问题。](https://example.invalid/q/4)
可以通过模块化和命名空间规避。

7.linux常用吗
常用 ls、grep 和 awk。

8.用户研究分为哪两个方面
定性研究和定量研究。
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert any("PreparedStatement" in q for q in qs)
    assert any("如何规避 JavaScript" in q for q in qs)
    assert any("linux常用吗" in q for q in qs)
    assert any("用户研究分为哪两个方面" in q for q in qs)


def test_inline_hierarchical_question_splits_from_previous_answer_with_exact_source_spans():
    md = """## MongoDB

11.3.0 如何查看使用 MongoDB 的连接？
使用命令 "db.adminCommand(connPoolStats)" 11.3.1 什么是复制？
复制是将数据同步到多个节点。
"""
    v2 = chunker.chunk_v2(md)
    assert [x["q"] for x in v2["items"]] == [
        "如何查看使用 MongoDB 的连接？", "什么是复制？"]
    assert "db.adminCommand" in v2["items"][0]["a"]
    assert "db.adminCommand" not in v2["items"][1]["q"]
    assert "复制是将数据同步" in v2["items"][1]["a"]
    for item in v2["items"]:
        start, end = item["span"]
        assert 0 <= start < end <= len(md)
        assert md[start:end]


def test_compatibility_toc_marker_without_page_numbers_ends_at_first_body_heading():
    md = """# Java 面试题

⽬录

必看

如何学习本项目

一、备战面试

1.1 如何准备面试

1.1.1 如何获取大厂面试机会？

二、Java 基础

## 1. 面向对象和面向过程的区别
面向过程按步骤组织程序，面向对象按对象组织行为。

## 2. Java 语言有哪些特点？
Java 有丰富的类库并支持面向对象。
"""
    v2 = chunker.chunk_v2(md)
    assert [x["q"] for x in v2["items"]] == [
        "面向对象和面向过程的区别", "Java 语言有哪些特点？"]
    assert "如何准备面试" not in "\n".join(x["q"] for x in v2["items"])


def test_repeated_hierarchical_outline_entry_without_answer_is_not_duplicated():
    md = """## MySQL

4.1.12 大表优化

4.1.13 解释一下什么是池化设计思想？

4.1.14 分库分表之后，id 主键如何处理？

详细正文开始。

4.1.13 解释一下什么是池化设计思想？
池化设计用于复用线程、数据库连接等资源。

4.1.14 分库分表之后，id 主键如何处理？
可以使用全局唯一 ID 方案。
"""
    v2 = chunker.chunk_v2(md)
    assert sum("池化设计思想" in x["q"] for x in v2["items"]) == 1
    assert sum("分库分表之后" in x["q"] for x in v2["items"]) == 1
    assert any("池化设计用于复用" in x["a"] for x in v2["items"] if "池化设计思想" in x["q"])


def test_http_version_number_is_not_an_inline_hierarchical_question_boundary():
    md = """## HTTP 面试

### HTTP/1.1 的性能
HTTP/1.1 使用长连接。

那你再说下 HTTP/1.1 的性能如何？
HTTP/1.1 通过长连接和管道化减少连接开销。

11.3.1 什么是复制？
复制是将数据同步到多个节点。
"""
    v2 = chunker.chunk_v2(md)
    q = [x["q"] for x in v2["items"]]
    assert "那你再说下 HTTP/1.1 的性能如何？" in q
    assert "的性能如何？" not in q
    http_item = next(x for x in v2["items"] if x["q"] == "那你再说下 HTTP/1.1 的性能如何？")
    assert "长连接和管道化" in http_item["a"]
    assert "什么是复制？" in q


def test_multiple_choice_numbered_stems_are_questions_not_lists():
    md = """# 单项选择题
1、若串 str=\"xunlei\",其子串的数目是（）
A、32 B、21 C、16 D、28
2、以下关于指针的说法,正确的是（）
A、指针是地址 B、指针可以为空
3、以下不是 RDBMS 的是（）
A、mysql B、Oracle C、Redis D、SQL Server
4、假设磁盘上的物理块大小是 4KB，利用率可达到（）
A、50% B、75% C、90% D、100%
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert len(qs) == 4
    assert any("RDBMS" in q for q in qs)
    assert any("xunlei" in q for q in qs)
    assert all("A、" not in q for q in qs)


def test_wrapped_question_continuation_is_not_misfiled_as_answer():
    md = """### 32、接口是否可继承（extends）接口？抽象类是否可实现

 （implements）接口？抽象类是否可继承具体类（concrete

class）？

答：
接口可以继承接口，而且支持多重继承。抽象类可以实现接口，也可以继承具体类。
"""
    v2 = chunker.chunk_v2(md)
    assert len(v2["items"]) == 1
    q = v2["items"][0]["q"]
    a = v2["items"][0]["a"]
    assert "implements" in q
    assert "concrete class" in q
    assert "接口可以继承接口" in a
    assert "implements" not in a
    assert "concrete" not in a


def test_numbered_answer_subpoints_under_parent_are_not_questions():
    """答案要点在父题下重启编号（## 1、/## 2、）不是新题；顺延编号的题头仍是题。"""
    md = """## 38、Redis 相比 Memcached 有哪些优势？

## 1、只有一个文件 dump.rdb，方便持久化。

## 2、容灾性好，一个文件可以保存到安全的磁盘。

## 3、队列

Reids 提供 list 和 set 操作，能作为消息队列平台。

## 39、使用过 Redis 做异步队列么，你是怎么用的？

答：一般使用 list 结构作为队列。
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert len(v2["items"]) == 2
    assert "Redis 相比 Memcached" in qs[0]
    assert "异步队列" in qs[1]
    assert not any("dump.rdb" in q or "容灾性" in q or q.strip() == "队列" for q in qs)
    assert "dump.rdb" in v2["items"][0]["a"]
