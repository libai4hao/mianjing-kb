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


def test_doc_to_text_disables_word_macros_before_open(monkeypatch, tmp_path):
    """旧 .doc COM 路径必须先 ForceDisable，再打开不可信文档。"""
    import sys
    import types

    seen = {}

    class FakeDoc:
        def SaveAs2(self, out, FileFormat):
            Path(out).write_bytes(b"safe text")
        def Close(self, _save):
            seen["closed"] = True

    class FakeDocuments:
        def Open(self, path, **kwargs):
            seen["security_at_open"] = word.AutomationSecurity
            seen["kwargs"] = kwargs
            return FakeDoc()

    class FakeWord:
        Visible = True
        DisplayAlerts = 1
        AutomationSecurity = 0
        Documents = FakeDocuments()
        def Quit(self):
            seen["quit"] = True

    word = FakeWord()
    client = types.ModuleType("win32com.client")
    client.DispatchEx = lambda name: word
    package = types.ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)

    src = tmp_path / "legacy.doc"
    src.write_bytes(b"not parsed by fake COM")
    assert cv.doc_to_text(src) == "safe text"
    assert seen["security_at_open"] == 3
    assert seen["kwargs"]["UpdateLinks"] == 0
    assert seen["kwargs"]["ConfirmConversions"] is False
    assert seen["closed"] and seen["quit"]


def test_convert_one_rejects_manifest_path_escape(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("不可读取", encoding="utf-8")
    entry = tmp_path / "entry.json"
    result = tmp_path / "result.json"
    entry.write_text(json.dumps({"rel": "../outside.txt", "category": "题库", "topic": "x",
                                 "title": "x.txt", "ext": ".txt"}), encoding="utf-8")
    assert cv.main([str(entry), str(result), "--corpus-root", str(corpus)]) == 1
    assert "不安全源路径" in json.loads(result.read_text(encoding="utf-8"))["err"]
    assert not (tmp_path / "20-题库MD").exists()


def test_convert_one_rejects_untrusted_category(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "src").mkdir(parents=True)
    (corpus / "src" / "x.txt").write_text("Redis 缓存问题", encoding="utf-8")
    entry = tmp_path / "entry.json"
    result = tmp_path / "result.json"
    entry.write_text(json.dumps({"rel": "src/x.txt", "category": "../../outside", "topic": "x",
                                 "title": "x.txt", "ext": ".txt"}), encoding="utf-8")
    assert cv.main([str(entry), str(result), "--corpus-root", str(corpus)]) == 1
    assert "非法 category" in json.loads(result.read_text(encoding="utf-8"))["err"]
    assert not (tmp_path / "outside").exists()


def test_docx_xml_member_size_limit(tmp_path):
    from mianjing_kb.kb.docx_safety import UnsafeDocx, read_docx_member
    p = tmp_path / "large.docx"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", "x" * 100)
    with pytest.raises(UnsafeDocx, match="大小超限"):
        read_docx_member(p, "word/document.xml", max_bytes=32)


def test_docx_archive_size_checked_before_zip_parse(tmp_path, monkeypatch):
    from mianjing_kb.kb import docx_safety
    archive = tmp_path / "oversized.docx"
    # sparse file：不实际写入 256 MiB，但验证在解析 ZIP 中央目录前拒绝。
    with archive.open("wb") as fh:
        fh.truncate(docx_safety.MAX_DOCX_ARCHIVE_BYTES + 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("ZipFile must not open an oversized archive")

    monkeypatch.setattr(docx_safety.zipfile, "ZipFile", forbidden)
    with pytest.raises(docx_safety.UnsafeDocx, match="压缩包文件大小超限"):
        docx_safety.read_docx_member(archive, "word/document.xml")


def test_paths_safe_join_rejects_ads_and_symlink_escape(tmp_path):
    from mianjing_kb.kb import paths
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(paths.UnsafePath, match="ADS"):
        paths.safe_join(root, "file.txt:stream")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台/权限不支持创建目录符号链接")
    with pytest.raises(paths.UnsafePath, match="越界"):
        paths.safe_join(root, "link/escaped.txt")


def test_question_freq_docx_member_is_size_limited(tmp_path):
    from mianjing_kb.question_freq import text_of_docx
    from mianjing_kb.kb.docx_safety import UnsafeDocx
    p = tmp_path / "oversized.docx"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", "x" * (32 * 1024 * 1024 + 1))
    with pytest.raises(UnsafeDocx):
        text_of_docx(p)


def test_convert_one_sanitizes_topic_to_single_directory(tmp_path):
    corpus = tmp_path / "corpus"
    src_dir = corpus / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "x.txt").write_text("什么是 Redis 缓存？", encoding="utf-8")
    entry, result = tmp_path / "entry.json", tmp_path / "result.json"
    entry.write_text(json.dumps({"rel": "src/x.txt", "category": "题库",
                                 "topic": "../../outside", "title": "x.txt", "ext": ".txt"}),
                     encoding="utf-8")
    assert cv.main([str(entry), str(result), "--corpus-root", str(corpus)]) == 0
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["ok"] is True
    assert (corpus / "20-题库MD" / "题库" / "_.._outside" / "x.md").is_file()
    assert not (tmp_path / "outside").exists()
