# -*- coding: utf-8 -*-
"""问题切分：题干要吃进来、答案要砍掉，近重复要能合并。"""
import zipfile

from mianjing_kb.question_freq import (
    cluster, grams, hamming, jaccard, normalize, simhash, split_questions, text_of_docx,
)

def test_split_keeps_question_drops_answer():
    text = (
        "1. 什么是 JVM？答：JVM 是 Java 虚拟机，负责把 class 翻译成机器码。\n"
        "2、说一下 GC 原理\n答案：分代回收 + 可达性分析\n"
        "3) 请写出线程池的核心参数\n"
        "页码 3\n"
        "4. 说明：这段是答案不是题目\n"
    )
    qs = split_questions(text)
    assert qs[0] == "什么是 JVM"
    assert "GC 原理" in qs[1]
    assert any("线程池的核心参数" in q for q in qs)
    # "说明：..." 后面没有疑问词 → 不该被当成题目
    assert all("这段是答案不是题目" not in q for q in qs)


def test_close_word_quits_com_and_is_idempotent(monkeypatch):
    """Word COM 全局单例必须显式 Quit——此前从不释放，extract/cluster 各跑一次留一只 WINWORD。"""
    from mianjing_kb import question_freq as qf

    class FakeApp:
        def __init__(self):
            self.quit_called = 0

        def Quit(self):
            self.quit_called += 1

    app = FakeApp()
    monkeypatch.setattr(qf, "_WORD", app)
    qf.close_word()
    assert app.quit_called == 1 and qf._WORD is None
    qf.close_word()                     # 幂等：已关不再重复 Quit
    assert app.quit_called == 1


def test_close_word_noop_without_instance():
    from mianjing_kb import question_freq as qf

    qf._WORD = None
    qf.close_word()                     # 没起过 Word 时是空操作，不抛异常
    assert qf._WORD is None



def test_normalize_handles_spacing_and_nfkc():
    assert normalize("什么 是 JVM ？") == normalize("什么是JVM？")
    assert normalize("1、MySQL 索引：B+树") != normalize("B+树")
    assert normalize("⼤⼚缓存") == normalize("大厂缓存")
    assert normalize("什么是⼤数据") == "什么是大数据"
    assert normalize("ＭｙＳＱＬ索引") == "mysql索引"
    assert normalize("㐀扩展A") == "㐀扩展a"


def test_normalize_keeps_code_symbols_distinct():
    assert normalize("C++") == "cpp"
    assert normalize("C#") == "csharp"
    assert normalize("i++") == "ipostinc"
    assert normalize("++i") == "preinci"
    assert normalize("i--") == "ipostdec"
    assert normalize("--i") == "predeci"
    assert normalize("A && B") == "aandb"
    assert normalize("A & B") == "abitandb"


def test_normalize_keeps_single_infix_operators_distinct():
    """#9：a+b / a-b / a*b / a/b / a<b / a>b 归一后不得撞成同串。"""
    forms = ["a+b", "a-b", "a*b", "a/b", "a<b", "a>b", "a%b", "a^b"]
    norms = [normalize(x) for x in forms]
    assert len(set(norms)) == len(forms)
    assert normalize("a+b") == "aplusb"
    assert normalize("a-b") == "aminusb"
    # 自然语言里的连字符/标点不受影响（两侧不全是字母数字）
    assert normalize("HashMap-Hashtable") == "hashmapminushashtable"  # 代码形态仍映射
    assert normalize("什么是 JVM？") == "什么是jvm"
    assert normalize("B+树") == "b树" or normalize("B+树") != normalize("B-树")


def test_near_duplicates_hash_close_far_ones_apart():
    a = simhash(normalize("说一下 JVM 的内存区域划分"))
    b = simhash(normalize("说说 JVM 的内存区域是怎么划分的"))
    c = simhash(normalize("MySQL 的隔离级别有哪几种"))
    # 短问句的 SimHash 抖动大（同义改写实测差 24 位），所以只当候选；判"同一题"靠 Jaccard 复核。
    # 复核阈值取 0.7 是**高精度优先**：复制粘贴时带题号/空格的变体能到 0.9+，
    # 真·同义改写（如"说一下…"vs"说说…是怎么…的"）只有 0.42，会被放过 —— 这是有意的。
    assert hamming(a, b) <= 24
    assert hamming(a, c) > 24          # 无关题必须拉得开，否则 LSH 候选会被噪声淹没
    assert jaccard(grams(normalize("1、说一下 JVM 的内存区域划分？")),
                   grams(normalize("说一下 JVM 的内存区域划分"))) >= 0.9
    assert jaccard(grams(normalize("说一下 JVM 的内存区域划分")),
                   grams(normalize("MySQL 的隔离级别有哪几种"))) < 0.2


def test_docx_keeps_auto_numbering_and_soft_break(tmp_path):
    """DOCX 的 1./2. 是 w:numPr 自动编号，不在 w:t 里；丢掉会让 chunker 少识别题号。"""
    doc = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        "<w:r><w:t>什么是 JVM</w:t><w:br/><w:t>第二行</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        "<w:r><w:t>说一下 GC</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    numbering = (
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    p = tmp_path / "a.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/numbering.xml", numbering)
    text = text_of_docx(p)
    assert "1. 什么是 JVM\n第二行" in text
    assert "2. 说一下 GC" in text


def test_cluster_uses_jaccard_not_simhash_hamming():
    """同题不同措辞即使 SimHash 差 63 位也必须进候选；旧 16bit LSH 会直接漏掉。"""
    items = [
        ("什么是jvm内存区域划分", 0),
        ("什么是jvm内存区域划分啊", (1 << 63) - 1),
    ]
    assert jaccard(grams(normalize(items[0][0])), grams(normalize(items[1][0]))) >= 0.9
    assert hamming(items[0][1], items[1][1]) > 24
    dsu = cluster(items, max_dist=24, min_jac=0.9)
    assert dsu.find(0) == dsu.find(1)


def test_cluster_uses_representative_star_not_transitive_closure():
    texts = [
        "mysql索引为什么用b+树不用b树",
        "mysql索引为什么用b+树",
        "为什么mysql索引用b+树",
        "为什么mysql索引使用b+树",
    ]
    assert jaccard(grams(texts[0]), grams(texts[1])) >= 0.7
    assert jaccard(grams(texts[1]), grams(texts[2])) >= 0.7
    assert jaccard(grams(texts[2]), grams(texts[3])) >= 0.7
    assert jaccard(grams(texts[0]), grams(texts[3])) < 0.7

    dsu = cluster([(text, 0) for text in texts], max_dist=24, min_jac=0.7)
    assert dsu.find(0) == dsu.find(1) == dsu.find(2)
    assert dsu.find(3) != dsu.find(0)
    assert texts[dsu.find(0)] == texts[1]
    for i in (0, 2):
        assert jaccard(grams(texts[i]), grams(texts[1])) >= 0.7


def test_cluster_star_invariant_holds_for_input_order_and_duplicates():
    from itertools import permutations

    texts = ["mysql索引为什么用b+树不用b树", "mysql索引为什么用b+树",
             "为什么mysql索引用b+树", "为什么mysql索引使用b+树"]
    for order in permutations(texts):
        items = [(text, 0) for text in order] + [(order[0], 0)]
        dsu = cluster(items, max_dist=24, min_jac=0.7)
        groups = {}
        for i in range(len(items)):
            groups.setdefault(dsu.find(i), []).append(i)
        assert len(groups) == 2
        for representative, members in groups.items():
            for i in members:
                assert jaccard(grams(items[representative][0]), grams(items[i][0])) >= 0.7
