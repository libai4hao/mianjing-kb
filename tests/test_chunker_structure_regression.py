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


def test_outline_run_missing_body_is_not_emitted():
    """P2-2 (S0500)：章内目录区（连续层级编号、正文缺失）不产出空答案题目。"""
    md = """## MyBatis 面试题总结

5.2.1 #{}和${}的区别是什么？

5.2.2 Xml 映射文件中，除了常见的 select|insert 标签，还有很多动态 sql 标签

5.2.3 Mybatis 是如何进行分页的？分页插件的原理是什么？

5.2.4 Mybatis 是否支持延迟加载？如果支持，它的实现原理是什么？

5.2.5 Mybatis 都有哪些 Executor 执行器？它们之间的区别是什么？

5.2.6 Mybatis 中如何指定使用哪一种 Executor 执行器？

5.2.7 Mybatis 是否可以映射 Enum 枚举类？

5.2.8 为什么说 Mybatis 是半自动 ORM 映射工具？它与全自动的区别在哪里？
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert not any("Executor" in q for q in qs)
    assert not any("#{}" in q for q in qs)


def test_outline_run_with_body_keeps_body_questions():
    """P2-2 守卫：目录区后接正文区时，正文题目保留、目录条目不再重复产出。"""
    md = """## MyBatis 面试题总结

5.2.1 #{}和${}的区别是什么？

5.2.2 Mybatis 是如何进行分页的？

5.2.3 Mybatis 都有哪些 Executor 执行器？

然后进入正文。

5.2.2 Mybatis 是如何进行分页的？
Mybatis 使用 RowBounds 进行内存分页，也可以使用 PageHelper 分页插件。

5.2.3 Mybatis 都有哪些 Executor 执行器？
有 SimpleExecutor、ReuseExecutor、BatchExecutor 三种，默认是 SimpleExecutor。
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert sum("分页" in q for q in qs) == 1
    assert sum("Executor" in q for q in qs) == 1
    assert any("RowBounds" in x["a"] for x in v2["items"] if "分页" in x["q"])
    assert any("SimpleExecutor" in x["a"] for x in v2["items"] if "Executor" in x["q"])


def test_numbered_task_questions_override_answer_list_state():
    md = """14.Kafka 与传统消息系统之间有三个关键区别

(1).Kafka 持久化日志，这些日志可以被重复读取和无限期保留

15.Kafka 创建 Topic 时如何将分区放置到不同的 Broker 中

(1).副本因子不能大于 Broker 的个数

16.Kafka 新建的分区会在哪个目录下创建

在启动 Kafka 集群之前，需要配置 log.dirs 参数。

17.partition 的数据如何保存到硬盘

topic 中的多个 partition 以文件夹形式保存。

18、讲讲 kafka 维护消费状态跟踪的方法

Kafka 通过 offset 标记消费状态。

19.kafka 的 ack 机制

request.required.acks 有三个值。

20.Kafka 的消费者如何消费数据

消费者记录消费的物理偏移量。

21.消费者负载均衡策略

一个消费者组中的一个分片对应一个消费者成员。

22.数据有序

一个消费者组内部有序，组之间无序。

23.kafaka 生产数据时数据的分组策略

key 决定数据生产到哪个 partition。

24.Kafka中的消息是否会丢失和重复消费？

需要从发送和消费两方面分析。
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    for text in ("创建 Topic", "哪个目录", "保存到硬盘", "维护消费状态", "ack 机制",
                 "如何消费数据", "负载均衡策略", "数据有序", "分组策略"):
        assert any(text in q for q in qs), text
    first = v2["items"][0]
    assert "持久化日志" in first["a"]
    assert "创建 Topic" not in first["a"]
    assert "Kafka中的消息是否会丢失" in qs[-1]


def test_number_only_markers_arm_following_question_stem():
    md = """# 阿里巴巴笔试

1.

如图所示，从A点发出一束激光，那么最多反射次数是____。

A 6 B 10 C 12

2.

假定CSomething是一个类，执行下面这些语句之后，内存里创建了____个对象。

A 10 B 9 C 8

### 3.

检查程序，是否存在问题，如果存在指出问题所在，如果不存在，说明输出结果。

A 0 B 1 C 2
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert len(v2["items"]) == 3, qs
    assert any("最多反射次数" in q for q in qs)
    assert any("创建了____个对象" in q for q in qs)
    assert any("检查程序" in q for q in qs)
    assert all(q.strip() for q in qs)


def test_numbered_formula_question_is_not_code():
    md = """1. 什么是 JVM？

JVM 是 Java 虚拟机。

3. 假设某商品在下一时刻无人浏览的概率为0.35，定义条件概率为P(O_{t+1}=0|O_t=1)=0.35，则t=100000时有人浏览的概率是____。

A 0.5371 B 0.4582 C 0.6316

4. 猜数字游戏的过程是你输入一个4位数，电脑会告诉你0A4B，那么最少还要猜____次。

A 0 B 1 C 2
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert any("条件概率" in q for q in qs), qs
    formula = next(x for x in v2["items"] if "条件概率" in x["q"])
    assert "0.5371" in formula["a"]
    assert any("猜数字游戏" in q for q in qs)


def test_inline_numbered_question_splits_from_code_and_options():
    md = """3. 检查程序，是否存在问题，如果存在指出问题所在，如果不存在，说明输出结果。

**public class Inc { public static void main(String[] args) { int i = 0; i = i++; } }** A 0 B 1 C 2 D 3 4. 检查程序，是否存在问题，如果存在指出问题所在，如果不存在，说明输出结果。 **public class Example { public static void main(String[] args) { } }**

A test ok B test bad

5. 下列关于Spring特性中IoC描述错误的是：

- A IoC是控制反转
- B IoC是依赖注入
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert len([q for q in qs if "检查程序" in q]) == 2, qs
    checks = [x for x in v2["items"] if "检查程序" in x["q"]]
    assert "class Inc" in checks[0]["a"]
    assert "class Example" not in checks[0]["a"]
    assert "class Example" in checks[1]["a"]
    assert any("Spring特性" in q for q in qs)


def test_inline_markers_and_fill_blank_questions_split_exam_tail():
    md = """16. Linux下两个进程可以同时打开同一个文件，这时如下描述错误的是：

- A 两个进程分别产生两个独立的fd
- F 一个进程的修改另一个进程可以立即感知

## 17.

考虑以下二分查找的代码：

**int bsearch(int array[], int n, int v) { int left = 0; int right = n - 1; }** 对于输入array为{2, 6, 8, 10}，n=4，v=10时，while循环调用的次数为____。 A 1 B 3 C 4 D 5 E 6 F 无数次 18.

对于输入array为{2, 6, 8, 10}，n=4，v=10时，while循环调用的次数为____。

在一个axb的整数矩阵中，寻找最长的严格递减数字序列，该问题的最优复杂度是____。举例来说，以下是一个3x5的矩阵，其结果如下：

A O(M*N) B O(M+N) C O(MlogN)

19. 设一棵二叉树有3个叶子节点，有8个度为1的节点，则该二叉树中总的节点数为______。 A 12 B 13 C 14 D 15 E 16 F 17 20. 一台机器对200个单词进行排序花了200秒，那么花费800秒，大概可以对多少个单词进行排序 A 400 B 500 C 600

D 700 E 800 F 900
"""
    v2 = chunker.chunk_v2(md)
    qs = [x["q"] for x in v2["items"]]
    assert any("二分查找" in q for q in qs), qs
    assert sum("while循环调用的次数" in q for q in qs) == 1, qs
    assert any("整数矩阵" in q for q in qs), qs
    assert any("二叉树" in q for q in qs), qs
    assert any("200个单词" in q for q in qs), qs
    q17 = next(x for x in v2["items"] if "二分查找" in x["q"])
    q18 = next(x for x in v2["items"] if "while循环调用的次数" in x["q"])
    matrix = next(x for x in v2["items"] if "整数矩阵" in x["q"])
    tree = next(x for x in v2["items"] if "二叉树" in x["q"])
    sorting = next(x for x in v2["items"] if "200个单词" in x["q"])
    assert "18." not in q17["a"]
    assert "整数矩阵" not in q18["a"]
    assert "二叉树" not in matrix["a"]
    assert "200个单词" not in tree["a"]
    assert "D 700" in sorting["a"]


def test_numbered_code_count_lines_are_not_questions():
    md = """1. 什么是计数器？

计数器用于记录数量。

1. int count = 0; // 当前多少

2. int limit = 10; // 最多多少

2. 计数器溢出怎么办？

检查边界。
"""
    items = chunker.chunk_v2(md)["items"]
    qs = [x["q"] for x in items]
    assert len(items) == 2, qs
    assert not any("int count" in q or "int limit" in q for q in qs)
    assert "int count" in items[0]["a"] and "int limit" in items[0]["a"]


def test_answer_list_labels_and_count_statements_stay_merged():
    md = """## 1、说说 Redis 集群

答案要点：

1. 索引优化

建立合适的索引。

2. 作用：保证数据一致

3. 当前最多可以保存多少个键

由配置决定。
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 1, [x["q"] for x in items]
    answer = items[0]["a"]
    assert "作用：保证数据一致" in answer
    assert "当前最多可以保存多少个键" in answer


def test_number_only_marker_does_not_arm_plain_answer_text():
    md = """1. 什么是缓存？

缓存用于加速读取。

2.

这里只是普通答案说明，不是新的题干。

3. 什么是数据库？

数据库用于持久化存储。
"""
    items = chunker.chunk_v2(md)["items"]
    qs = [x["q"] for x in items]
    assert not any("普通答案说明" in q for q in qs), qs
    cache = next(x for x in items if "缓存" in x["q"])
    assert "普通答案说明" in cache["a"]
    assert any("数据库" in q for q in qs)


def test_inline_answer_subheading_is_not_split_after_options():
    md = """1. 以下哪些描述正确？

A 支持持久化 B 支持分片 2. 实现方式：主从复制

主从复制是答案小节。
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 1, [x["q"] for x in items]
    assert "实现方式：主从复制" in items[0]["a"]


def test_inline_code_tail_keeps_long_answer_spans_aligned():
    md = ("3. 检查程序，是否存在问题。\n\n"
          "**public class Inc {}** A 0 B 1 C 2 D 3 "
          "4. 检查程序，是否存在问题。 **public class Example { public static void main(String[] args) { } }**\n\n"
          + "\n\n".join(f"第{i}段：" + "详细答案内容。" * 80 for i in range(1, 8))
          + "\n\n5. 下一题\n\n下一答案。\n")
    blocks = chunker.chunk(md, split_long=True)["blocks"]
    continuations = [b for b in blocks if b["qid"] == 4 and b["style"].endswith("/续")]
    assert continuations
    for block in continuations:
        start, end = block["source_span"]
        assert md[start:end] == block["a_text"]


def test_math_task_stems_override_code_punctuation():
    md = """1. 假设某事件概率为P(A)=0.35，定义条件概率为P(B|A)=0.65，则后续概率是____。

A 0.35 B 0.65

2. 猜数字游戏输入0789后得到0A4B，那么最少还要猜____次。

A 0 B 1
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 2, [x["q"] for x in items]
    assert "条件概率" in items[0]["q"]
    assert "猜数字游戏" in items[1]["q"]


def test_long_fill_blank_candidate_splits_after_options():
    stem = "对于输入array为{2, 6, 8, 10}，运行查找函数，循环调用的次数为____。"
    stem = ("重复说明：" * 32) + stem
    assert len(stem) > 180
    md = f"""1. 考虑以下查找代码：

int search(int n) {{ return n; }}

A 1 B 2 C 3 2.

{stem}

A 1 B 2
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 2, [x["q"] for x in items]
    assert items[1]["q"] == stem
    assert "A 1 B 2" in items[1]["a"]
