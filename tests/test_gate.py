# -*- coding: utf-8 -*-
"""gate 的端到端行为：健康 md 通过，坏字体 md 被拦，短文判 non_content。"""
import json

from gate import main


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
    assert out["verdict"]["pass"] == 1
    assert out["verdict"]["garbled"] == 1
    assert out["verdict"]["non_content"] == 1


def test_gate_report_file(tmp_path, capsys):
    md = tmp_path / "md"
    write(md / "a.md", "中文内容占绝大多数，这里只是测试报告落盘。" * 20)
    out_file = tmp_path / "report.json"
    main(["--md-dir", str(md), "--no-db", "--out", str(out_file)])
    capsys.readouterr()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["summary"]["files"] == 1
    assert data["results"][0]["md_path"].endswith("a.md")
