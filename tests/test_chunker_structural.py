# -*- coding: utf-8 -*-
"""切题器结构回归：目录区、代码区、题号状态、答案列表、Markdown 标题边界。"""
from mianjing_kb import chunker


def test_toc_region_does_not_emit_questions():
    md = """# 题库

#### 内容涵盖：Java、Redis、MySQL

目录

#### 1、什么是A？.............................................................. 1

 1、什么是A？.............................................................. 2

#### 2、什么是B？.............................................................. 3

 2、什么是B？.............................................................. 4

#### 3、什么是C？.............................................................. 5

第1 页共3 页

-----

#### 4、什么是D？.............................................................. 6

第2 页共3 页

-----

#### 5、什么是E？.............................................................. 7

第3 页共3 页

-----

## 正文

### 1、什么是A？

答案A

### 2、什么是B？

答案B
"""
    v2 = chunker.chunk_v2(md)
    assert [it["q"] for it in v2["items"]] == ["什么是A？", "什么是B？"]
    assert not any(it["span"][0] < md.find("## 正文") for it in v2["items"])


def test_develop_code_region_and_real_tasks():
    md = """思路：利用栈的思想。

1. #include <stdio.h>

2. #include <string.h>

5. char * convert_path_opt （const char * path ）

6. {

7. char * result = NULL ;

8. int top = 0 ;

9. int path_len = 0 ，

10. index = 0 ，

11. point_cnt = 0 ;

14. if （NULL == path ）

15. {

16. fprintf （stderr ，“invalid argument。\\ n”

）;

17. 返回 NULL ;

18. }

1. 2. 比较两个字符串的不同，返回不同点的位置（相对第二字符串而言）

1. #include <stdio.h>

4. char * compare_strings （const char * plhs ，

5. const char * prhs ）

6. {

7. int lhs_index = 0 ，

8. rhs_index = 0 ;

27. return prhs + rhs_index ;

28. }

42.3. 给定一个 100 亿的整数文件，输出前 100 个最小值。

1. #include <stdio.h>

2. int main （）

3. {

4. return 0 ;

5. }

4. 通过随机函数生成随机数填充一个二维数组，随机数被 4 取余
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert len(qs) == 3, qs
    assert "比较两个字符串的不同" in qs[0]
    assert "给定一个 100 亿" in qs[1]
    assert "通过随机函数" in qs[2]
    assert not any(chunker.is_code_fragment(q) for q in qs)
    assert any(p["why"] == "孤立代码区" for p in v2["probe"])


def test_markdown_numbered_heading_breaks_previous_answer():
    md = """#### 9、一个线程如果出现了运行时异常会怎么样

异常没有被捕获时线程会停止。

#### 11、sleep方法和wait方法有什么区别

sleep 不释放监视器，wait 会释放。

#### 12、生产者消费者模型的作用是什么

解耦和平衡产能。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert qs == ["一个线程如果出现了运行时异常会怎么样",
                  "sleep方法和wait方法有什么区别",
                  "生产者消费者模型的作用是什么"]
    assert "sleep方法和wait" not in v2["items"][0]["a"]


def test_question_inside_answer_before_next_heading_gets_empty_answer():
    md = """#### 19、ConcurrentHashMap的并发度是什么

并发度就是 segment 的大小。

Hashtable能同时有两条线程获取Hashtable中的数据吗？

#### 20、ReadWriteLock是什么

读写锁的内容。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert "Hashtable能同时有两条线程获取Hashtable中的数据吗？" in qs
    hashtable = next(it for it in v2["items"] if it["q"].startswith("Hashtable"))
    assert hashtable["a"].strip() == ""
    assert "ReadWriteLock" not in hashtable["a"]


def test_acid_numbered_list_is_not_questions():
    md = """**ACID**

指数据库事务正确执行的四个基本要素：

1. 原子性（Atomicity）

2. 一致性（Consistency）

3. 隔离性（Isolation）

4. 持久性（Durability）

**CAP**

CAP 原则指一致性、可用性、分区容忍性。
"""
    v2 = chunker.chunk_v2(md)
    assert not any("持久性" in it["q"] for it in v2["items"])
    assert not any("原子性" in it["q"] for it in v2["items"])


def test_section_heading_is_not_a_title_question():
    md = """#### 内容涵盖：Java、Redis

目录

#### 1、Q1.............................................................. 1

#### 2、Q2.............................................................. 2

第1 页共2 页

-----

## MyBatis 面试题

### 1、什么是Mybatis？

答案。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert "MyBatis 面试题" not in qs
    assert "内容涵盖：Java、Redis" not in qs
    assert "什么是Mybatis？" in qs


def test_short_numbered_interview_topics_are_questions():
    """面经里的无问号短话题（虚函数实现机制/纯虚函数）不能被当成答案列表吞掉。"""
    md = """阿里一面（部门组长面）

1. 简单自我介绍了下就开始问项目

2. 虚函数实现机制（问的比较细）

3. 纯虚函数

阿里二面 总监面

1. 微信获取权限的协议？

2. 关于共享内存 mmap 映射文件。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert "虚函数实现机制（问的比较细）" in qs
    assert "纯虚函数" in qs
    assert "微信获取权限的协议？" in qs


def test_answer_list_after_title_question_is_not_questions():
    """标题型母题后的 1./2. 编号列表是答案，不是新题。"""
    md = """## 18，熟悉哪些 Redis 集群模式？

1. Redis Sentinel

体量较小时选用单主 Redis。

2. Redis Cluster

官方集群化方案，通过分片使用更多内存。

3. Twemprox

代理服务器，减少连接数量。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert len(qs) == 1
    assert qs[0].endswith("熟悉哪些 Redis 集群模式？")
    assert not any(q in ("Redis Sentinel", "Redis Cluster", "Twemprox") for q in qs)


def test_markdown_answer_subheadings_do_not_split_questions():
    """`## N、知识点汇总/本地方法栈` 是答案小节，不能成为独立题。"""
    md = """## 1、ConcurrentHashMap 为何读不加锁。

读操作不加锁。

## 2、HashMap 如何同步。

使用 synchronizedMap 包装。

## 3、说说常见的集合有哪些吧。

Map 和 Collection。

## 两者的主要区别如下：

- 第一点
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert "ConcurrentHashMap 为何读不加锁。" in qs
    assert "HashMap 如何同步。" in qs
    assert "说说常见的集合有哪些吧。" in qs
    assert "两者的主要区别如下：" not in qs


def test_embedded_hierarchy_number_and_reference_link_are_split():
    """`...图11.1.7 什么是 mongod？` / `参考：URL 11.3.0 如何...` 只保留题干。"""
    md = """11.1.6 MongoDB 和关系型数据库术语对比图11.1.7 什么是“mongod”？

mongod 是处理 MongoDB 系统的主要进程。

参考：https://docs.mongodb.com/manual/sharding/ 11.3.0 如何查看使用 MongoDB 的连接？

使用 db.adminCommand("connPoolStats")。
"""
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert "什么是“mongod”？" in qs
    assert "如何查看使用 MongoDB 的连接？" in qs
    assert not any("docs.mongodb.com" in q for q in qs)
    assert not any("11.1.7" in q for q in qs)


