# -*- coding: utf-8 -*-
"""privacy_scan 规则与豁免标注的单元测试。"""

from mianjing_kb import privacy_scan as ps  # noqa: E402


def _hits(tmp_path, monkeypatch, rel, text):
    monkeypatch.setattr(ps, "REPO", tmp_path)
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return ps.scan([rel])


def test_abs_path_and_secret_are_flagged(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "a.py", 'P = r"D:\\Project\\x"\nKEY = "sk-' + "a" * 24 + '"\n')  # privacy-scan: allow 合成测试数据
    rules = {h["rule"] for h in hits}
    assert "abs_path" in rules and "secret" in rules


def test_allow_marker_skips_line(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "b.py",
                 'phone = "13812345678"  # privacy-scan: allow 合成\n')
    assert hits == []


def test_noreply_github_mail_is_fine(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "c.md", "作者 <libai4hao@users.noreply.github.com>\n")
    assert hits == []


def test_placeholder_is_fine(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "d.md", "语料根：<CORPUS_ROOT>\\20-题库MD\n")
    assert hits == []


def test_abs_path_any_drive_not_just_whitelist(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "e.md",
                 "临时目录：D:\\tmp\\x\n输出：E:\\work\\out\\a.log\n")  # privacy-scan: allow 合成测试数据
    assert [h["rule"] for h in hits] == ["abs_path", "abs_path"]


def test_abs_path_single_segment_and_custom_dir(tmp_path, monkeypatch):
    """带盘符就够：D: 下自定义目录、系统目录都不再靠目录白名单才命中。"""
    hits = _hits(tmp_path, monkeypatch, "f.md",
                 "语料放在 D:\\corpus\n工具在 C:\\Program Files\\PowerShell\n")  # privacy-scan: allow 合成测试数据
    assert [h["rule"] for h in hits] == ["abs_path", "abs_path"]




def test_scan_staged_reads_index_blob(tmp_path, monkeypatch):
    """--staged 判的是将要提交的 blob：工作区改干净也不能绕过。"""
    monkeypatch.setattr(ps, "REPO", tmp_path)
    (tmp_path / "h.py").write_text("clean\n", encoding="utf-8")
    monkeypatch.setattr(ps, "_staged_text",
                        lambda rel: 'KEY = "sk-' + "a" * 24 + '"\n')  # privacy-scan: allow 合成测试数据
    assert [h["rule"] for h in ps.scan(["h.py"], staged=True)] == ["secret"]
    assert ps.scan(["h.py"]) == []          # 非 staged 仍按工作区（干净）


def test_scan_reports_skipped_files(tmp_path, monkeypatch):
    """跳过的文件必须有账：否则"扫了 0 个"也会被当成干净。"""
    monkeypatch.setattr(ps, "REPO", tmp_path)
    (tmp_path / "big.py").write_text("x" * (600 * 1024), encoding="utf-8")
    (tmp_path / "img.png").write_text("x", encoding="utf-8")
    stats = {}
    assert ps.scan(["big.py", "img.png"], stats=stats) == []
    whys = {s["file"]: s["why"] for s in stats["skipped"]}
    assert "超过 512KB" in whys["big.py"] and "非文本" in whys["img.png"]
