# -*- coding: utf-8 -*-
"""gate 的端到端行为：健康 md 通过，坏字体 md 判 fail(乱码)，短文判 fail(过短)。

P0-2 适配（2026-11-17 审核）：gate.py 改三态（pass/review/fail）+ rows 结构后，
原四态断言（garbled/non_content）相应改为 fail 的两种 reason。
"""
import json

from mianjing_kb.gate import main


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_gate_verdicts(tmp_path, capsys):
    md = tmp_path / "md"
    healthy = ("# JVM 内存模型\n\n堆、方法区、虚拟机栈、本地方法栈、程序计数器。"
               "垃圾回收器：Serial、Parallel、CMS、G1、ZGC。G1 把堆划分为 Region，"
               "按停顿时间目标挑选回收收益最高的 Region，称为优先列表。") * 12
    write(md / "healthy.md", healthy)
    write(md / "garbled.md", "5.2̵ ړ\u0af2ୗ᭗ᦔᶎᦶෆቘ " * 20)
    write(md / "short.md", "太短了")

    assert main(["--md-dir", str(md), "--no-db"]) == 0
    out = json.loads(capsys.readouterr().out.split("\n\n--- ")[0])

    assert out["files"] == 3
    assert out["passed"] == 1
    assert out["failed"] == 2          # garbled → fail(乱码)；short → fail(过短)
    assert out["review"] == 0


def test_gate_report_rows(tmp_path, capsys):
    md = tmp_path / "md"
    write(md / "a.md", "中文内容占绝大多数，这里只是测试报告落盘。" * 20)
    out_file = tmp_path / "report.json"
    main(["--md-dir", str(md), "--no-db", "--out", str(out_file)])
    capsys.readouterr()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    # P0-2：报告结构 = {summary, thresholds, rows, collisions}，与 dispatch/ocr_merge 消费口径一致
    assert data["summary"]["files"] == 1
    assert data["rows"][0]["md"].endswith("a.md")
    assert data["rows"][0]["state"] == "pass"
    assert data["thresholds"]["nfkc"] is True


def test_sentinel_data_shape():
    """哨兵回归集数据形状（离线校验；判定行为本身由 --sentinel 对生产语料跑）。"""
    from mianjing_kb.gate import CASES, PAIRS
    assert len(CASES) == 13 and len(PAIRS) == 2
    assert len({rel for rel, _ in CASES}) == 13                      # 无重复
    assert all(w in ("pass", "review", "fail") for _, w in CASES)
    assert all(w in ("同件", "不同件") for w, _a, _b in PAIRS)


def test_metrics_ignores_whitespace_page_markers():
    from mianjing_kb.gate import metrics
    raw = "===== page 1 =====\n" + "中文内容" * 30 + "\n" * 200
    m = metrics(raw)
    assert m["chars"] == 120          # 空白 / 分页标记不计入长度
    assert m["fixed"] >= 0            # 不再用 -1 冒充计数


def test_bad_chars_allow_greek_emoji_but_reject_box_drawing():
    from mianjing_kb.gate import metrics
    assert metrics("λ Θ Ω μ " * 20)["bad"] == 0.0
    assert metrics("😀" * 10)["bad"] == 0.0
    assert metrics("■ □ " * 10)["bad"] == 1.0


def test_md_path_keeps_dotted_stem(tmp_path, monkeypatch):
    from mianjing_kb import gate
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", tmp_path)
    e = {"category": "题库", "topic": "Java", "title": "v3.0-JavaGuide面试突击版.pdf",
         "rel": "x.pdf"}
    assert gate.md_path_for(e).name == "v3.0-JavaGuide面试突击版.md"


def test_resolve_md_uses_hash_variant(tmp_path, monkeypatch):
    from mianjing_kb import gate
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", tmp_path)
    target = tmp_path / "题库" / "Java" / "foo__deadbeef.md"
    write(target, "中文" * 100)
    e = {"category": "题库", "topic": "Java", "title": "foo.pdf", "rel": "x.pdf",
         "doc_hash": "deadbeef00"}
    assert gate.resolve_md(e, {}) == target


def test_resolve_md_does_not_bind_unrelated_same_name(tmp_path, monkeypatch):
    from mianjing_kb import gate
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", tmp_path / "md")
    other = tmp_path / "other" / "README.md"
    write(other, "无关内容")
    e = {"category": "题库", "topic": "Java", "title": "README.pdf", "rel": "x.pdf",
         "doc_hash": "deadbeef00"}
    idx = {"readme.md": [str(other)]}
    assert gate.resolve_md(e, idx) is None


def test_collisions_ignore_missing_and_duplicate_log_rows():
    from mianjing_kb.gate import collisions_of
    missing = [dict(rel="a.pdf", md="x.md", exists=False, doc_hash="a"),
               dict(rel="b.pdf", md="x.md", exists=False, doc_hash="b")]
    assert collisions_of(missing) == []
    dup = [dict(rel="a.pdf", md="x.md", exists=True, doc_hash="a"),
           dict(rel="a.pdf", md="x.md", exists=True, doc_hash="a")]
    assert collisions_of(dup) == []
    same = [dict(rel="a.pdf", md="x.md", exists=True, doc_hash="a"),
            dict(rel="b.pdf", md="x.md", exists=True, doc_hash="a")]
    assert collisions_of(same)[0]["kind"] == "同内容重复"
    real = [dict(rel="a.pdf", md="x.md", exists=True, doc_hash="a"),
            dict(rel="b.pdf", md="x.md", exists=True, doc_hash="b")]
    assert collisions_of(real)[0]["kind"] == "真覆盖(丢内容)"


def test_sentinel_all_miss_is_failure(tmp_path, monkeypatch, capsys):
    from mianjing_kb import gate
    monkeypatch.setattr(gate.config, "BUILD_DIR", tmp_path)
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", tmp_path / "md")
    monkeypatch.setattr(gate.config, "CORPUS_ROOT", tmp_path / "corpus")
    (tmp_path / "registry.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(gate, "CASES", [(r"missing\a.pdf", "pass")])
    monkeypatch.setattr(gate, "PAIRS", [])
    assert gate.run_sentinel() is False
    assert "防护触发" in capsys.readouterr().out


def test_audit_skips_unregistered_rows(tmp_path, monkeypatch):
    from mianjing_kb import gate
    build = tmp_path / "build"
    corpus = tmp_path / "corpus"
    md_root = tmp_path / "md"
    build.mkdir(); corpus.mkdir(); md_root.mkdir()
    monkeypatch.setattr(gate.config, "BUILD_DIR", build)
    monkeypatch.setattr(gate.config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", md_root)
    e = {"rel": "src\\foo.pdf", "title": "foo.pdf", "category": "题库", "topic": "Java"}
    (build / "registry.json").write_text(json.dumps([e], ensure_ascii=False), encoding="utf-8")
    (build / "convert_log.json").write_text(json.dumps([
        {"rel": "src\\foo.pdf", "ok": True},
        {"rel": "src\\missing.pdf", "ok": True},
    ], ensure_ascii=False), encoding="utf-8")
    write(md_root / "题库" / "Java" / "foo.md", "中文内容" * 60)
    report = gate.run_audit()
    assert report["summary"]["converted"] == 2
    assert report["summary"]["examined"] == 1
    assert report["summary"]["skipped_no_registry"] == 1


def test_audit_hash_variant_is_not_drift(tmp_path, monkeypatch):
    """F5：同目录、同 stem、hash8 对得上 doc_hash 的 md 是正常产物，不算 drift。"""
    from mianjing_kb import gate
    build, corpus, md_root = tmp_path / "build", tmp_path / "corpus", tmp_path / "md"
    for d in (build, corpus, md_root):
        d.mkdir()
    monkeypatch.setattr(gate.config, "BUILD_DIR", build)
    monkeypatch.setattr(gate.config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", md_root)
    e = {"rel": "src\\foo.pdf", "title": "foo.pdf", "category": "题库", "topic": "Java",
         "doc_hash": "deadbeef00"}
    (build / "registry.json").write_text(json.dumps([e], ensure_ascii=False), encoding="utf-8")
    (build / "convert_log.json").write_text(
        json.dumps([{"rel": "src\\foo.pdf", "ok": True}], ensure_ascii=False), encoding="utf-8")
    write(md_root / "题库" / "Java" / "foo__deadbeef.md", "中文内容" * 60)
    report = gate.run_audit()
    row = report["rows"][0]
    assert row["md"].endswith("foo__deadbeef.md")
    assert row["drift"] is False
    assert report["summary"]["drift"] == 0


def test_audit_different_document_same_name_is_still_drift(tmp_path, monkeypatch):
    """同名但落在别的目录、且确实指向同一 doc_hash → 仍记 drift（不能一并放宽）。"""
    from mianjing_kb import gate
    build, corpus, md_root = tmp_path / "build", tmp_path / "corpus", tmp_path / "md"
    for d in (build, corpus, md_root):
        d.mkdir()
    monkeypatch.setattr(gate.config, "BUILD_DIR", build)
    monkeypatch.setattr(gate.config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(gate.config, "QUESTION_MD_DIR", md_root)
    e = {"rel": "src\\foo.pdf", "title": "foo.pdf", "category": "题库", "topic": "Java",
         "doc_hash": "deadbeef00"}
    (build / "registry.json").write_text(json.dumps([e], ensure_ascii=False), encoding="utf-8")
    (build / "convert_log.json").write_text(
        json.dumps([{"rel": "src\\foo.pdf", "ok": True}], ensure_ascii=False), encoding="utf-8")
    # 约定位置是 题库/Java/foo.md，实际同名文件漂到 面经/Java/（内容里带 doc_hash 才会被绑定）
    write(md_root / "面经" / "Java" / "foo.md", "deadbeef00\n" + "中文内容" * 60)
    report = gate.run_audit()
    assert report["rows"][0]["drift"] is True
    assert report["summary"]["drift"] == 1


def test_gate_and_textmetrics_definitions_do_not_drift():
    from mianjing_kb.gate import cross_check
    raw = "# 标题\n\n" + "中文内容，包含标点。ABC 123 " * 30
    c = cross_check(raw)
    assert c["cjk_delta"] < 0.02
    assert c["visible_delta"] < 40


def test_extract_gate_filter_blocks_review_and_fail():
    from mianjing_kb.extract import _filter_by_gate
    statuses = ({"a": "garbled"}, {"b": "low_cjk", "c": "pass"})
    kept, skipped = _filter_by_gate([("a", "a"), ("b", "b"), ("c", "c"), ("d", "d")],
                                    statuses, require_gate=False)
    assert [x[0] for x in kept] == ["c", "d"]
    assert skipped == {"garbled": 1, "low_cjk": 1}
    kept, skipped = _filter_by_gate([("a", "a"), ("d", "d")], statuses, require_gate=True)
    assert kept == []
    assert skipped == {"garbled": 1, "ungated": 1}
