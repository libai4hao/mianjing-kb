# -*- coding: utf-8 -*-
"""端到端链路测试：用仓库自带合成语料跑 接入 → 分类 → 切题 → 建库 → 门禁 → 检索。

与 test_*.py 里的大量纯函数用例互补：这里真的起子进程、真的建库、真的跑门禁。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import demo  # noqa: E402


def test_demo_pipeline_end_to_end(tmp_path):
    res = demo.run(tmp_path / "demo", quiet=True)
    assert res["ok"], res
    steps = res["steps"]
    assert steps["intake"]["summary"]["actions"]["added"] >= 4      # 5 源件里 1 份是重复
    assert steps["intake"]["summary"]["actions"]["duplicate"] >= 1  # 内容哈希判重生效
    assert res["counts"]["docs_classified"] >= 5
    assert res["counts"]["questions"] >= 20
    assert res["counts"]["doc_contents"] >= 5
    gates = steps["verify_ledger"]["summary"]["gates"]
    assert "FAIL" not in gates.values(), gates
    assert steps["search"]["hits"] >= 1
    assert Path(res["db"]).exists()


def test_demo_does_not_touch_real_corpus(tmp_path, monkeypatch):
    """demo 必须只写 --work 目录：语料副本在 work/corpus 下。"""
    res = demo.run(tmp_path / "demo2", quiet=True)
    assert Path(res["corpus"]).parts[-2:] == ("demo2", "corpus")
    assert Path(res["db"]) == tmp_path / "demo2" / "build" / "kb.sqlite"


def test_demo_is_repeatable_in_same_work_dir(tmp_path):
    """复用 --work 时必须清掉上次的 build，否则计数会累加、门禁读到旧库。"""
    work = tmp_path / "demo3"
    a = demo.run(work, quiet=True)
    b = demo.run(work, quiet=True)
    assert b["ok"]
    assert a["counts"] == b["counts"]
