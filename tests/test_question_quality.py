# -*- coding: utf-8 -*-
"""题目输入质量预检与切题防回归。"""
from mianjing_kb.quality_rules import code_fragment_reason
from mianjing_kb.chunker import chunk_v2


def test_code_fragment_patterns_from_pilot100_are_detected():
    fragments = [
        "#include <stdio.h>",
        "char * convert_path_opt (const char * path )",
        "char * result = NULL ;",
        "int top = 0 ;",
        "int path_len = 0 ,",
        "index = 0 ,",
        "/ ** <check * /",
        "if (NULL == path )",
        "fprintf (stderr , invalid_argument )",
        "返回 NULL ;",
        "path_len = strlen (path );",
        "while (index < path_len )",
        "index < path_len && path [ index ] == '。' ;",
        "result [ top ++] = path [ index ++];",
        "++ point_cnt ,++ index );",
        "返回结果;",
    ]
    assert all(code_fragment_reason(x) for x in fragments)


def test_natural_language_code_questions_are_not_rejected():
    valid = [
        "解释 ++i 和 i++ 有什么区别？",
        "分析以下代码的输出结果：int main(){ return 0; }",
        "写一个函数，遍历文件夹下的所有文件和文件夹。",
        "sizeof(P) 在 32 位编译器下是多少？",
    ]
    assert all(code_fragment_reason(x) is None for x in valid)


def test_numbered_code_listing_is_not_split_into_questions():
    lines = [
        "#include <stdio.h>",
        "char * convert_path_opt (const char * path )",
        "char * result = NULL ;",
        "int top = 0 ;",
        "if (NULL == path )",
        "返回 NULL ;",
        "path_len = strlen (path );",
        "result [ top ++] = path [ index ++];",
        "++ point_cnt ,++ index );",
        "返回结果;",
    ]
    md = "\n\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    parsed = chunk_v2(md)
    assert parsed["items"] == []
    assert parsed["probe"]
    assert all("代码片段" in x["why"] for x in parsed["probe"])


def test_code_inside_a_natural_language_question_remains_answer_context():
    md = "分析以下代码输出什么？\n\n```c\nint main(){ return 0; }\n```\n"
    parsed = chunk_v2(md)
    assert len(parsed["items"]) == 1
    assert "int main" in parsed["items"][0]["a"]


def test_all_pilot100_v1_code_rows_are_quarantined():
    # q_text is copied from the 31 sequential pilot records S070–S100.
    # Keep this explicit fixture so future regex/prompt changes do not let
    # the same code block back into the labeling queue.
    pilot_fragments = [
        "#include <stdio.h>",
        "#include <string.h>",
        "#include <stdlib.h>",
        "char * convert_path_opt (const char * path )",
        "char * result = NULL ;",
        "int top = 0 ;",
        "int path_len = 0 ,",
        "index = 0 ,",
        "point_cnt = 0 ;",
        "/ ** <check * /",
        "if (NULL == path )",
        "fprintf (stderr ,“convert_path_opt:invalid argument。\\ n”",
        "返回 NULL ;",
        "/ ** <分配内存* /",
        "path_len = strlen (path );",
        "result = (char *)malloc (path_len * sizeof (char ));",
        "if (NULL == 结果)",
        "fprintf (stderr ,“convert_path_opt:无法访问 malloc。\\",
        "/ ** <convert * /",
        "while (index < path_len )",
        "/ ** <点之前复制字符。* /",
        "while (index < path_len && path [ index ] != '。' )",
        "result [ top ++] = path [ index ++];",
        "for (point_cnt = 0 ;",
        "index < path_len && path [ index ] == '。' ;",
        "++ point_cnt ,++ index );",
        "if (point_cnt == 2 )",
        "while (- top > 0 && result [ top ] != '/' );",
        "++ index ;",
        "结果[ top ] = '\\ 0' ;",
        "返回结果;",
    ]
    assert len(pilot_fragments) == 31
    assert all(code_fragment_reason(text) for text in pilot_fragments)


def test_pilot_mismatched_answers_and_section_fragments_are_held():
    from mianjing_kb.quality_rules import deterministic_quality_flags

    cases = [
        ("你平时会看哪些方面的书,对哪些方面比较感兴趣?",
         "回答兴趣后说明不要说没有兴趣。\n\n4、32 位 JVM 可以跑多大的程序?\n\n5、HTTP POST 和 GET 有何区别?",
         "多题混杂"),
        ("基于商户模块提出一个需求,问怎么去设计?", "（二）技术篇:", "答案错配"),
        ("数字证书的作用,里面有什么内容?", "（三）腾讯的群面面经：产品策划/运营方向。", "答案错配"),
        ("根本∶这个问题是否触达本质?", "", "残缺"),
        ("资源∶我们有什么可以动用的资源、方法?", "", "残缺"),
        ("如何解决: 1.给float元素添加display:inline", "2.给ie6写一个hack", "残缺"),
        ("如下程序,则在 32 位编译器下 sizeof(P)为()", "A、4 B、8 C、12 D、16", "残缺"),
        ("下面说法正确的是()", "A、一个空类默认生成构造函数\nB、可以有多个析构函数", "残缺"),
        ("大题(五道选两道完成就可以了)", "算概率\n写出三种设计模式\n描述 TCP 三次握手", "多题混杂"),
        ("在带头结点的双向循环链表中插入一个新结点，需要修改指针域数量？",
         "参考答案\n1、D 2、C 3、D 4、C 5、D 6、B 7、C", "多题混杂"),
        ("什么是 MVC? 用过吗?", "数据库设计新浪笔试题:", "答案错配"),
    ]
    for q_text, a_text, expected in cases:
        assert expected in deterministic_quality_flags(q_text, a_text), (q_text, a_text)


def test_more_pilot_contamination_patterns_are_detected():
    from mianjing_kb.quality_rules import deterministic_quality_flags

    cases = [
        ("假如有实习经验,他会问你实习中做过的项目,然后他会找你一些你项目中的", "", "残缺"),
        ("get 传送的数据量较小,不能大于2KB。post 传送的数据量较大,一般被", "", "残缺"),
        ("get 安全性非常低,post 安全性较高。但是执行效率却比Post 方法好。", "", "残缺"),
        ("可以搭建坐标系,辅助思考 5、四个出发点:需求、根本、资源、roj 1)需求", "", "多题混杂"),
        ("上来先让介绍一个最近做的项目", "然后他问用过 webpack 没。然后问 js 闭包。又问 HTTP 缓存。接着问 Vue。", "多题混杂"),
        ("做表单类页面需要注意哪些问题?", "font-face是什么\n实现一个圆环进度条\n页面效果切换如何实现\n闭包是什么\nJS原型和原型链", "多题混杂"),
        ("写一个函数,可以遍历文件夹下的所有文件和文件夹。", "7、8、中间有几个 unix shell 的题目\n9、有 mail.log 的文档\n10、js 中网页前进和后退的代码\n11、window.open 如何调用父窗口", "多题混杂"),
    ]
    for q_text, a_text, expected in cases:
        assert expected in deterministic_quality_flags(q_text, a_text), (q_text, a_text)
