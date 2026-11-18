# -*- coding: utf-8 -*-
"""batch_label 离线测试：目标筛选 / 打标落 staging / 断点续跑 / 预算停 / 抽查 / G6 merge。"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import batch_label as bl  # noqa: E402
import llm_client as lc  # noqa: E402
from tools.kb import config  # noqa: E402

REAL_META = config.META_DIR


def _fake_label(obj=None, fail_qids=()):
    """返回 label_one 替身：fail_qids 中的题抛异常，其余返回固定打标。"""
    def fake(cfg, q):
        if q["qid"] in fail_qids:
            raise RuntimeError("LLM 调用重试 3 次耗尽")
        return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
                "topic": (obj or {}).get("topic", "Java基础"),
                "qtype": (obj or {}).get("qtype", "八股"),
                "difficulty": (obj or {}).get("difficulty", 3),
                "tags": json.dumps((obj or {}).get("tags", ["HashMap"]), ensure_ascii=False),
                "provider": cfg["provider"], "model": cfg["model"], "redacted": 0,
                "latency_ms": 10, "tokens": 100, "labeled_at": "now"}
    return fake


@pytest.fixture()
def mini_db(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    from tools.kb import db as kbdb
    conn = kbdb.connect(build / "kb.sqlite")      # 完整迁移（005 staging + 006 题级 topic 列）
    rows = [(1, "什么是 HashMap？", ""), (2, "什么是 Redis？", ""),
            (3, "手写 LRU？", ""), (4, "已有主题的题", "")]
    for qid, q_text, _ in rows:
        topic = "MySQL" if qid == 4 else ""
        conn.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, "
                     "q_norm, q_norm_hash, simhash, span_start, span_end, topic, extracted_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (qid, f"h{qid}", qid, q_text, "答案内容" * 20, "八股", "high",
                      q_text, "x", 0, 0, 1, topic, "now"))
    conn.commit()
    yield conn
    conn.close()


CFG = {"provider": "online", "model": "m", "base": "http://x/v1", "key": "k",
       "concurrency": 2, "timeout": 1.0, "json_mode": True, "price": (1.0, 4.0)}


def test_pick_targets_only_empty_topic(mini_db):
    targets = bl.pick_targets(mini_db)
    assert [t["qid"] for t in targets] == [1, 2, 3]      # qid=4 已有 MySQL 主题


def test_run_batch_writes_staging_and_resume(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    assert out["labeled"] == 3 and out["failed"] == 0
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 3
    # resume：staging 已有的 qid 跳过（再跑 todo=0）
    out2 = bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    assert out2["todo"] == 0 and out2["labeled"] == 0


def test_run_batch_failed_not_blocking(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label(fail_qids={2}))
    fp = tmp_path / "f.jsonl"
    out = bl.run_batch(CFG, mini_db, failed_path=fp)
    assert out["labeled"] == 2 and out["failed"] == 1
    assert "qid" in fp.read_text(encoding="utf-8")


def test_run_batch_budget_stops(mini_db, monkeypatch, tmp_path):
    """预算封顶（软上限）：超限后 cancel 未起飞的；飞行中的自然完成，总成本有界。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, budget=0.0005, failed_path=tmp_path / "f.jsonl")
    assert out["labeled"] <= 2                      # 并发 2：第 3 题被 cancel
    assert out["est_cost_yuan"] <= 0.0011            # 总成本有界（2 题 × 0.0005）


def test_dry_run_no_staging(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, dry_run=True, failed_path=tmp_path / "f.jsonl")
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 0


def test_sample_staging(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    rows = bl.sample_staging(mini_db, 2)
    assert len(rows) == 2 and all("q_text" in r and "topic" in r for r in rows)


def test_merge_g6_gate_blocks_and_passes(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    # G6 PASS → merge 落库
    out = bl.merge(mini_db)
    assert out["ok"] and out["merged"] == 3
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=1").fetchone()[0] == "Java基础"
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 0
    # staging 空 → 再 merge 报 G6 SKIP 拒绝
    out2 = bl.merge(mini_db)
    assert not out2["ok"] and out2["gate"] == "SKIP"


def test_merge_blocked_on_bad_schema(mini_db):
    """schema 不合格（qtype 越枚举）→ G6 FAIL → merge 拒绝，staging 保留。"""
    mini_db.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
                    "model, redacted, latency_ms, tokens, labeled_at) VALUES "
                    "(1,'x','玄学',9,'not-json','p','m',0,1,1,'now')")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert not out["ok"] and out["gate"] == "FAIL"
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 1


def test_staging_carries_q_norm_hash(mini_db, monkeypatch, tmp_path):
    """F1：staging 行带 q_norm_hash 且与 questions 一致（merge 校验与事实落档的键）。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    rows = mini_db.execute(
        "SELECT s.q_norm_hash AS sh, q.q_norm_hash AS qh FROM labels_staging s "
        "JOIN questions q ON q.qid=s.qid").fetchall()
    assert rows and all(r["sh"] == r["qh"] and r["sh"] for r in rows)


def test_redact_integration_in_label_one(monkeypatch):
    """label_one 的脱敏链路：题干含手机号 → redacted=1 且送 LLM 的文本已遮罩。"""
    sent = {}

    def fake_chat_json(cfg, system, user, **kw):
        sent["user"] = user
        return {"obj": {"topic": "HR", "qtype": "HR", "difficulty": 2, "tags": []},
                "latency_ms": 5, "tokens": 10}

    monkeypatch.setattr(bl, "chat_json", fake_chat_json)
    row = bl.label_one(CFG, {"qid": 9, "q_text": "自我介绍？电话 13812345678", "a_text": "答"})
    assert row["redacted"] == 1
    assert "13812345678" not in sent["user"]
    assert "[联系方式]" in sent["user"]
