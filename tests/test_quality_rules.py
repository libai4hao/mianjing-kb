# -*- coding: utf-8 -*-
"""确定性质量规则：pilot100 回归、自然语言误杀、CSV 导出前缀。"""
from mianjing_kb.quality_rules import (
    code_fragment_reason,
    deterministic_quality_flags,
    manual_review_flags,
)


def test_pilot100_code_fragments_are_detected():
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


def test_code_lines_with_explanation_words_are_still_code():
    """复核修复：提问词在行中出现不能把注释/返回语句放回题目队列。"""
    for text in ("// 输出格式说明", "return 输出结果;", "// 请求参数说明",
                 'fprintf(stderr, "说明");'):
        assert code_fragment_reason(text), text


def test_type_size_questions_are_not_declarations():
    """复核修复：int/char 类型占几个字节是面试题，不是代码声明。"""
    for text in ("int 类型占几个字节", "int 占几个字节",
                 "char 类型占几个字节", "long 占几个字节"):
        assert code_fragment_reason(text) is None, text


def test_natural_language_code_questions_are_kept():
    for text in (
        "解释 ++i 和 i++ 有什么区别？",
        "分析以下代码的输出结果：int main(){ return 0; }",
        "写一个函数，遍历文件夹下的所有文件和文件夹。",
        "sizeof(P) 在 32 位编译器下是多少？",
    ):
        assert code_fragment_reason(text) is None, text


def test_csv_formula_prefix_does_not_hide_code_fragment():
    assert code_fragment_reason("'++ point_cnt ,++ index );") == "increment_list"
    assert code_fragment_reason("'++ index ;") == "increment"


def test_pilot100_structural_contamination_is_held():
    cases = [
        ("假如有实习经验,他会问你实习中做过的项目,然后他会找你一些你项目中的", "", "残缺"),
        ("get 传送的数据量较小,不能大于2KB。post 传送的数据量较大,一般被", "", "残缺"),
        ("如何解决: 1.给float元素添加display:inline", "2.给ie6写一个hack", "残缺"),
        ("大题(五道选两道完成就可以了)",
         "算概率\n写出三种设计模式\n描述 TCP 三次握手", "多题混杂"),
        ("数字证书的作用,里面有什么内容?",
         "（三）腾讯的群面面经：产品策划/运营方向。", "答案错配"),
    ]
    for q_text, a_text, expected in cases:
        flags = deterministic_quality_flags(q_text, a_text)
        assert expected in flags, (q_text, a_text, flags)
        assert expected in manual_review_flags(flags)


def test_answer_key_and_numbered_multi_question_are_held():
    flags = deterministic_quality_flags(
        "在带头结点的双向循环链表中插入一个新结点，需要修改指针域数量？",
        "参考答案\n1、D 2、C 3、D 4、C 5、D 6、B 7、C")
    assert "多题混杂" in flags


def test_short_question_and_empty_answer_are_not_held_alone():
    flags = deterministic_quality_flags("首先是自我介绍。", "")
    assert flags == set()
    assert manual_review_flags(flags) == set()
