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
