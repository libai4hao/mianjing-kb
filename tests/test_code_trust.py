# -*- coding: utf-8 -*-
"""代码执行信任边界回归（审计候选 #13/#66/#79/#84，P1-F）。"""
import builtins
import json
import sys
from pathlib import Path

import pytest

from mianjing_kb import pilot5000 as P
from mianjing_kb import reconvert as rc
from mianjing_kb.kb import convert_one as cv

REPO = Path(__file__).resolve().parents[1]


def test_fallback_loads_exact_file_not_syspath(tmp_path, monkeypatch):
    """#13：仓内版不可用时，fallback 只按精确文件路径加载语料侧 convert_one.py；
    sys.path 上的同名抢占模块不得被执行。"""
    corpus = tmp_path / "corpus"
    tools = corpus / "21-面经数据库" / "tools"
    tools.mkdir(parents=True)
    (tools / "convert_one.py").write_text("MARK = 'corpus-fallback'\n", encoding="utf-8")
    # 抢占目录：sys.path 更靠前的同名模块（若被 import 即执行）
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "convert_one.py").write_text(
        "raise AssertionError('shadow module executed')\n", encoding="utf-8")
    monkeypatch.setattr(rc.config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(sys, "path", [str(shadow)] + list(sys.path))
    # 模拟仓内版缺失：替换包属性让 from-import 失败
    import mianjing_kb.kb as kbpkg
    monkeypatch.delattr(kbpkg, "convert_one", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "mianjing_kb.kb" and a and len(a) > 2 and a[2] and "convert_one" in a[2]:
            raise ModuleNotFoundError("mianjing_kb.kb.convert_one", name="mianjing_kb.kb.convert_one")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = rc._load_convert_one()
    assert getattr(mod, "MARK", None) == "corpus-fallback"
    assert str(tools) not in sys.path          # 语料 tools 未被塞进 sys.path


def test_fallback_missing_file_raises(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setattr(rc.config, "CORPUS_ROOT", corpus)
    import mianjing_kb.kb as kbpkg
    monkeypatch.delattr(kbpkg, "convert_one", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "mianjing_kb.kb" and a and len(a) > 2 and a[2] and "convert_one" in a[2]:
            raise ModuleNotFoundError("mianjing_kb.kb.convert_one", name="mianjing_kb.kb.convert_one")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        rc._load_convert_one()


def test_backfill_converter_prefers_repo():
    """#13：backfill 默认执行仓内 vendored 转换器，不是语料树脚本。"""
    from mianjing_kb import backfill_convert as bc
    assert bc.CONVERTER == bc._REPO_CONVERTER
    assert bc.CONVERTER.is_file()


def test_pilot_config_rejects_bad_python_and_repo(tmp_path):
    """#66：python 必须存在、repo_root 必须是真实仓库 checkout。"""
    cfg = {**P.DEFAULT_CONFIG, "llm": dict(P.DEFAULT_CONFIG["llm"])}
    cfg["python"] = str(tmp_path / "not-exists.exe")
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(RuntimeError, match="python"):
        P.ensure_config(p)
    cfg["python"] = sys.executable
    cfg["repo_root"] = str(tmp_path)          # 不是仓库
    p.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(RuntimeError, match="repo_root"):
        P.ensure_config(p)
    cfg["repo_root"] = str(REPO)
    p.write_text(json.dumps(cfg), encoding="utf-8")
    assert P.ensure_config(p)["repo_root"] == str(REPO)


def test_cut1000_rejects_foreign_repo():
    """#79：--repo 指向别的目录 → 拒绝（import 根锚定脚本所在仓库）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_cut1000_samples", REPO / "scripts" / "make_cut1000_samples.py")
    mc = importlib.util.module_from_spec(spec)
    sys.modules["make_cut1000_samples"] = mc
    spec.loader.exec_module(mc)
    with pytest.raises(SystemExit, match="仓库"):
        mc.main(["--db", "x", "--md-root", "y", "--out", "z", "--repo", "/nonexistent-repo"])


def test_convert_one_cli_refuses_doc_without_watchdog(tmp_path):
    """#84：convert_one 直跑入口拒绝 .doc（无看门狗），指向 doc_convert。"""
    corpus = tmp_path / "corpus"
    (corpus / "src").mkdir(parents=True)
    (corpus / "src" / "a.doc").write_bytes(b"fake doc")
    entry = tmp_path / "e.json"
    result = tmp_path / "r.json"
    entry.write_text(json.dumps({"rel": "src/a.doc", "category": "题库", "topic": "x",
                                 "title": "a.doc", "ext": ".doc"}), encoding="utf-8")
    assert cv.main([str(entry), str(result), "--corpus-root", str(corpus)]) == 1
    out = json.loads(result.read_text(encoding="utf-8"))
    assert "doc_convert" in out["err"]


def test_text_of_doc_runs_in_subprocess_with_timeout(tmp_path, monkeypatch):
    """#84：question_freq 的 .doc 读取走子进程 + 硬超时；超时抛错不挂起。"""
    import mianjing_kb.question_freq as qf
    from mianjing_kb.kb import proc
    calls = {}

    def fake_run(argv, timeout, **k):
        calls["timeout"] = timeout
        calls["argv"] = argv
        return 1, "", "boom", True

    monkeypatch.setattr(proc, "run_with_timeout", fake_run)
    monkeypatch.setitem(sys.modules, "win32com", type(sys)("win32com"))
    monkeypatch.setitem(sys.modules, "win32com.client", type(sys)("win32com.client"))
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="超时"):
        qf.text_of_doc(p)
    assert calls["timeout"] == qf.DOC_COM_TIMEOUT
    assert calls["argv"][0] == sys.executable
