"""仓内 convert_one（T17）：不依赖语料侧 tools 也能转换，输出口径可回归。"""
import json
import zipfile
from pathlib import Path

import pytest

from mianjing_kb.kb import convert_one as cv

DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>JVM 面试题</w:t></w:r></w:p>
    <w:p><w:r><w:t>什么是 JVM？</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:r><w:t>题号</w:t></w:r></w:tc><w:tc><w:r><w:t>答案</w:t></w:r></w:tc></w:tr>
      <w:tr><w:tc><w:r><w:t>1</w:t></w:r></w:tc><w:tc><w:r><w:t>虚拟机</w:t></w:r></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""


def _docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", DOC)


def test_safe_name_and_cjk_ratio():
    assert cv.safe_name('a/b:c*?"<>|') == "a_b_c" + "_" * 6
    assert cv.safe_name("x" * 100) == "x" * 80
    assert cv.cjk_ratio("中文abc") == pytest.approx(2 / 5)
    assert cv.cjk_ratio("") == 0.0


def test_docx_to_md_renders_heading_and_table(tmp_path):
    p = tmp_path / "a.docx"
    _docx(p)
    md = cv.docx_to_md(p)
    assert md.startswith("# JVM 面试题")
    assert "什么是 JVM？" in md
    assert "| 题号 | 答案 |" in md


def test_cli_requires_explicit_corpus_root(monkeypatch):
    monkeypatch.delenv("KB_CORPUS_ROOT", raising=False)
    with pytest.raises(SystemExit):
        cv.corpus_root(None)
    assert cv.corpus_root(r"C:\corpus") == Path(r"C:\corpus")


def test_cli_converts_txt_without_corpus_side_tools(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "31-收件箱").mkdir(parents=True)
    (corpus / "31-收件箱" / "a.txt").write_text("什么是 Redis？\n\n缓存", encoding="utf-8")
    entry = tmp_path / "entry.json"
    result = tmp_path / "result.json"
    entry.write_text(json.dumps({"rel": "31-收件箱/a.txt", "category": "题库", "topic": "Redis",
                                 "title": "a", "ext": ".txt"}), encoding="utf-8")
    monkeypatch.delenv("KB_CORPUS_ROOT", raising=False)
    assert cv.main([str(entry), str(result), "--corpus-root", str(corpus)]) == 0
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["ok"] is True and out["chars"] > 0
    assert (corpus / "20-题库MD" / "题库" / "Redis" / "a.md").exists()


def test_reconvert_prefers_repo_converter(monkeypatch):
    """reconvert 的 loader 必须解析到仓内版本（语料侧不存在也不影响）。"""
    from mianjing_kb import reconvert as rc
    monkeypatch.setattr(rc.config, "CORPUS_ROOT", Path("<CORPUS_ROOT>"))
    assert rc._load_convert_one() is cv
    assert rc._converter_source(cv) == "repo"
