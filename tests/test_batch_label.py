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


def test_cli_flags_provider_concurrency_resume(tmp_path, monkeypatch, capsys):
    """F10：--provider/--concurrency/--resume 三旗标 docs/15 有承诺，敲命令不得 unrecognized。

    --provider/--concurrency 写 os.environ 后再 resolve_config；--resume 是 no-op 兼容
    旗标（续跑本就是默认行为：staging 已有 qid 自动跳过）。
    """
    import os
    seen = {}

    def fake_resolve_config():
        seen["provider"] = os.environ.get("LLM_PROVIDER")
        seen["concurrency"] = os.environ.get("LLM_CONCURRENCY")
        return {"provider": "local", "base": "http://x", "model": "m", "key": "",
                "concurrency": 2, "timeout": 1, "json_mode": False, "price": (1.0, 4.0)}

    monkeypatch.setattr(os, "environ", dict(os.environ))   # 隔离实现写入的环境变量
    monkeypatch.setattr(bl, "resolve_config", fake_resolve_config)
    monkeypatch.setattr(bl, "health", lambda cfg, conn: {"ok": True})
    rc = bl.main(["--provider", "local", "--concurrency", "2", "--resume",
                  "--check", "--db", str(tmp_path / "kb.sqlite")])
    assert rc == 0
    assert seen == {"provider": "local", "concurrency": "2"}


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
                "latency_ms": 10, "tokens": 100,
                "prompt_tokens": 80, "completion_tokens": 20, "labeled_at": "now"}
    return fake


def _add_question(conn, qid: int, q_text: str, topic: str = ""):
    conn.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, "
                 "q_norm, q_norm_hash, simhash, span_start, span_end, topic, extracted_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (qid, f"h{qid}", qid, q_text, "答案内容" * 20, "八股", "high",
                  f"norm{qid}", f"hash{qid}", 0, 0, 1, topic, "now"))


@pytest.fixture()
def mini_db(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    from tools.kb import db as kbdb
    conn = kbdb.connect(build / "kb.sqlite")      # 完整迁移（005 staging + 006 题级 topic 列）
    for qid, q_text in [(1, "什么是 HashMap？"), (2, "什么是 Redis？"),
                        (3, "手写 LRU？"), (4, "已有主题的题")]:
        _add_question(conn, qid, q_text, "MySQL" if qid == 4 else "")
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
    """预算封顶（软上限）：超限后 cancel 未起飞的；飞行中的自然完成，总成本有界。

    F5 新口径：单题成本 = (80 in × ¥1 + 20 out × ¥4)/1e6 = 0.00016；
    budget=0.0003 → 第 2 题完成后 0.00032 超限即停。
    """
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, budget=0.0003, failed_path=tmp_path / "f.jsonl")
    assert out["labeled"] <= 2                      # 并发 2：第 3 题被 cancel
    assert out["est_cost_yuan"] <= 0.0005           # 总成本有界（≤3 题 × 0.00016）


def test_run_batch_cost_splits_in_out(mini_db, monkeypatch, tmp_path):
    """bug4/F5：成本按 in/out 单价分开计（与 health 口径一致），staging 落两列。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    assert out["labeled"] == 3
    # 3 题 × (80 in × ¥1 + 20 out × ¥4) / 1e6 —— 不再 sum(price) 统一计（旧口径高估约 4 倍）
    assert out["est_cost_yuan"] == round(3 * (80 * 1.0 + 20 * 4.0) / 1e6, 4)
    row = mini_db.execute(
        "SELECT prompt_tokens, completion_tokens FROM labels_staging LIMIT 1").fetchone()
    assert row["prompt_tokens"] == 80 and row["completion_tokens"] == 20


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


def test_merge_writes_four_columns_and_facts(mini_db, monkeypatch, tmp_path):
    """F9+F1：merge 四列（topic/qtype/difficulty/tags）落库 + label_facts 按 hash 落档。"""
    monkeypatch.setattr(bl, "label_one", _fake_label(
        {"topic": "Redis", "qtype": "八股", "difficulty": 4, "tags": ["缓存"]}))
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    out = bl.merge(mini_db)
    assert out["ok"]
    row = mini_db.execute("SELECT topic, qtype, difficulty, tags FROM questions WHERE qid=1").fetchone()
    assert row["topic"] == "Redis" and row["qtype"] == "八股"
    assert row["difficulty"] == 4 and row["tags"] == '["缓存"]'
    facts = mini_db.execute("SELECT q_norm_hash, topic, difficulty FROM label_facts").fetchall()
    assert len(facts) == 3                                  # qid 1-3 各一条（hash 互异）
    assert {r["topic"] for r in facts} == {"Redis"}
    assert {r["q_norm_hash"] for r in facts} == {"hash1", "hash2", "hash3"}


def test_merge_empty_topic_keeps_existing(mini_db, monkeypatch, tmp_path):
    """F3：混入的空 topic staging 行不覆盖 questions 已有 topic（≤5% 场景 G6 放行）。"""
    for qid in range(5, 22):                                # 追加 17 题凑 20 行 staging
        _add_question(mini_db, qid, f"追加题{qid}")
    mini_db.commit()
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")   # 19 行（qid4 有主题不打）
    mini_db.execute("INSERT INTO labels_staging (qid, q_norm_hash, topic, qtype, difficulty, tags, "
                    "provider, model, redacted, latency_ms, tokens, labeled_at) VALUES "
                    "(4,'hash4','','八股',3,'[]','p','m',0,1,1,'now')")
    mini_db.commit()
    out = bl.merge(mini_db)                                 # ok_rate=19/20=0.95 → PASS
    assert out["ok"], out
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=4").fetchone()[0] == "MySQL"


def test_merge_refuses_orphan_rows(mini_db, monkeypatch, tmp_path):
    """F6：qid 已失效或题干已变（hash 不匹配）的 staging 行拒绝 merge 且 staging 保留。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")     # 3 行有效
    # 孤儿①：qid 不在 questions（跨 extract 残留）
    mini_db.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
                    "model, redacted, latency_ms, tokens, labeled_at) VALUES "
                    "(999,'X','八股',3,'[]','p','m',0,1,1,'now')")
    out = bl.merge(mini_db)
    assert not out["ok"] and out["orphans"] == 1
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 4  # 保留未清
    # 孤儿②：qid 在但 q_norm_hash 不匹配（重切后题干已变）
    mini_db.execute("DELETE FROM labels_staging WHERE qid=999")
    mini_db.execute("UPDATE labels_staging SET q_norm_hash='changed' WHERE qid=1")
    out2 = bl.merge(mini_db)
    assert not out2["ok"] and out2["orphans"] == 1
    # 历史 NULL hash 行放行（向后兼容，qid 对得上即可）
    mini_db.execute("UPDATE labels_staging SET q_norm_hash=NULL WHERE qid=1")
    out3 = bl.merge(mini_db)
    assert out3["ok"]


def test_merge_refusal_lists_orphan_qids(mini_db, monkeypatch, tmp_path):
    """评审 Important#2：拒绝输出列出孤儿 qid——只有计数时人工清理无从下手。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    mini_db.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
                    "model, redacted, latency_ms, tokens, labeled_at) VALUES "
                    "(999,'X','八股',3,'[]','p','m',0,1,1,'now')")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert not out["ok"] and out["orphan_qids"] == [999]


def test_merge_drop_orphans_cleans_and_merges(mini_db, monkeypatch, tmp_path):
    """评审 Important#2：drop_orphans 清掉孤儿行后继续 merge——重跑打标清不掉孤儿
    （run_batch 跳过 staging 已有 qid），没有这个出口主工作流会死锁。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")     # 3 行有效
    mini_db.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
                    "model, redacted, latency_ms, tokens, labeled_at) VALUES "
                    "(999,'X','八股',3,'[]','p','m',0,1,1,'now')")
    mini_db.commit()
    out = bl.merge(mini_db, drop_orphans=True)
    assert out["ok"] and out["merged"] == 3 and out["dropped_orphans"] == 1
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 0
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=1").fetchone()[0] == "Java基础"


def test_merge_next_step_hint(mini_db, monkeypatch, tmp_path):
    """F4/B2：merge 成功输出 next_step 提示 refresh——检索库 topic 是快照。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    out = bl.merge(mini_db)
    assert out["ok"] and "refresh.py" in out["next_step"]


def test_redact_integration_in_label_one(monkeypatch):
    """label_one 的脱敏链路：题干含手机号 → redacted=1 且送 LLM 的文本已遮罩。"""
    sent = {}

    def fake_chat_json(cfg, system, user, **kw):
        sent["user"] = user
        return {"obj": {"topic": "HR", "qtype": "HR", "difficulty": 2, "tags": []},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake_chat_json)
    row = bl.label_one(CFG, {"qid": 9, "q_text": "自我介绍？电话 13812345678", "a_text": "答"})
    assert row["redacted"] == 1
    assert "13812345678" not in sent["user"]
    assert "[联系方式]" in sent["user"]
    assert row["prompt_tokens"] == 7 and row["completion_tokens"] == 3   # F5 透传拆分计数


# ---- 批次二 G2：金标抽样/录入（docs/项目审核报告/修复方案-2026-11-19.md §八）----

def _stage(conn, qid: int, topic: str = "JVM", q_norm_hash: str | None = "X"):
    """往 staging 塞一行（q_norm_hash=None 显式造 NULL 历史行）。"""
    conn.execute("INSERT OR REPLACE INTO labels_staging "
                 "(qid, q_norm_hash, topic, qtype, difficulty, tags, provider, model, "
                 "redacted, latency_ms, tokens, prompt_tokens, completion_tokens, labeled_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (qid, f"hash{qid}" if q_norm_hash == "X" else q_norm_hash,
                  topic, "八股", 3, '["tag"]', "local", "m", 0, 1, 10, 8, 2, "now"))


def test_golden_sample_stratified_writes_todo(mini_db):
    """G2 --golden-sample：按 topic 分层轮转抽样（小主题不被淹没），写 build/golden_todo.json。"""
    for qid, topic in [(1, "JVM"), (2, "MySQL"), (3, "MySQL"), (4, "Redis")]:
        _stage(mini_db, qid, topic)
    out = bl.golden_sample(mini_db, 3)
    assert out["sampled"] == 3 and out["strata"] == 3
    data = json.loads((config.BUILD_DIR / "golden_todo.json").read_text(encoding="utf-8"))
    items = data["items"]
    assert len(items) == 3
    assert {it["current"]["topic"] for it in items} == {"JVM", "MySQL", "Redis"}  # 层覆盖
    it = items[0]
    assert {"qid", "q_norm_hash", "q_text", "a_text", "current",
            "topic_gold", "qtype_gold", "difficulty_gold"} <= set(it)
    assert it["topic_gold"] == "" and it["qtype_gold"] == "" and it["difficulty_gold"] is None


def test_golden_sample_skips_legacy_rows_without_hash(mini_db):
    """G2：staging 无 q_norm_hash 的历史行不进金标（金标以 hash 为键，跨重切存活）。"""
    _stage(mini_db, 1, "JVM")
    _stage(mini_db, 2, "MySQL", q_norm_hash=None)
    out = bl.golden_sample(mini_db, 5)
    assert out["sampled"] == 1
    assert out["items"][0]["q_norm_hash"] == "hash1"


def test_golden_load_validates_and_inserts(mini_db, tmp_path):
    """G2 --golden-load：填好的 todo 回灌 golden_labels；重复载入 REPLACE 幂等。"""
    _stage(mini_db, 1, "JVM")
    todo = {"items": [{"qid": 1, "q_norm_hash": "hash1", "q_text": "什么是 HashMap？",
                       "a_text": "…", "topic_gold": "Java基础", "qtype_gold": "八股",
                       "difficulty_gold": 3}]}
    p = tmp_path / "golden_todo.json"
    p.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
    out = bl.golden_load(mini_db, p, annotator="张三")
    assert out["ok"] and out["loaded"] == 1
    r = mini_db.execute("SELECT topic_gold, qtype_gold, difficulty_gold, annotator "
                        "FROM golden_labels WHERE q_norm_hash='hash1'").fetchone()
    assert tuple(r) == ("Java基础", "八股", 3, "张三")
    out2 = bl.golden_load(mini_db, p)                  # 默认 annotator=human
    assert out2["ok"] and out2["total"] == 1           # REPLACE：不重复累积
    assert mini_db.execute("SELECT annotator FROM golden_labels").fetchone()[0] == "human"


def test_golden_load_rejects_invalid_whole_batch(mini_db, tmp_path):
    """G2：topic_gold 空 / qtype_gold 越界 / difficulty_gold 越界 → 整批拒绝不落表。"""
    todo = {"items": [
        {"q_norm_hash": "hash1", "topic_gold": "JVM"},                     # 合法（gold 类型可空）
        {"q_norm_hash": "hash2", "topic_gold": ""},
        {"q_norm_hash": "hash3", "topic_gold": "JVM", "qtype_gold": "玄学"},
        {"q_norm_hash": "hash4", "topic_gold": "JVM", "difficulty_gold": 9},
    ]}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
    out = bl.golden_load(mini_db, p)
    assert not out["ok"] and out["rejected"] == 3 and len(out["errors"]) <= 10
    assert mini_db.execute("SELECT COUNT(*) FROM golden_labels").fetchone()[0] == 0
