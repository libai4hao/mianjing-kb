# -*- coding: utf-8 -*-
"""batch_label 离线测试：目标筛选 / 打标落 staging / 断点续跑 / 预算停 / 抽查 / G6 merge。"""
import json
from pathlib import Path

import pytest
from mianjing_kb import batch_label as bl  # noqa: E402
from mianjing_kb.kb import config  # noqa: E402

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
    from mianjing_kb.kb import db as kbdb
    db_path = tmp_path / "kb.sqlite"
    kbdb.connect(db_path).close()          # D-28：--check 只读，必须预先有库
    rc = bl.main(["--provider", "local", "--concurrency", "2", "--resume",
                  "--check", "--db", str(db_path)])
    assert rc == 0
    assert seen == {"provider": "local", "concurrency": "2"}


def _fake_label(obj=None, fail_qids=()):
    """返回 label_one 替身：fail_qids 中的题抛异常，其余返回固定打标（含 T21 新维度）。"""
    def fake(cfg, q):
        if q["qid"] in fail_qids:
            raise RuntimeError("LLM 调用重试 3 次耗尽")
        o = obj or {}
        pos = o.get("position", ["后端"])
        qual = o.get("quality")
        return {"qid": q["qid"], "q_norm_hash": q.get("q_norm_hash"),
                "topic": o.get("topic", "Java基础"),
                "qtype": o.get("qtype", "八股"),
                "difficulty": o.get("difficulty", 3),
                "canonical_q": o.get("canonical_q", q["q_text"][:60]),
                "position": json.dumps(pos, ensure_ascii=False) if pos else None,
                "quality": json.dumps(qual, ensure_ascii=False) if qual else None,
                "tag_norm_hits": o.get("tag_norm_hits", 0),
                "tags": json.dumps(o.get("tags", ["HashMap"]), ensure_ascii=False),
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
    from mianjing_kb.kb import db as kbdb
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


def test_staging_keeps_review_note_for_pending(mini_db, monkeypatch, tmp_path):
    """pending 行必须留"为什么 pending"（含模型原话 topic），否则人工终审只能看到"其他"。"""
    def fake(cfg, q):
        row = _fake_label()(cfg, q)
        row["review_state"] = "pending"
        row["review_note"] = json.dumps({"issues": ["topic_out_of_vocab:PHP"]},
                                        ensure_ascii=False)
        return row

    monkeypatch.setattr(bl, "label_one", fake)
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    note = mini_db.execute("SELECT review_note FROM labels_staging WHERE qid=1").fetchone()[0]
    assert "PHP" in note and "topic_out_of_vocab" in note


def test_run_batch_failed_not_blocking(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label(fail_qids={2}))
    fp = tmp_path / "f.jsonl"
    out = bl.run_batch(CFG, mini_db, failed_path=fp)
    assert out["labeled"] == 2 and out["failed"] == 1
    assert "qid" in fp.read_text(encoding="utf-8")


def test_failed_csv_is_human_todo(mini_db, monkeypatch, tmp_path):
    """失败件要有人工入口：jsonl 是明细，csv 按 qid 聚合出错误类型/尝试次数/模型。"""
    def boom(cfg, q):
        raise RuntimeError("JSON 解析失败")

    monkeypatch.setattr(bl, "label_one", boom)
    fp = tmp_path / "f.jsonl"
    out = bl.run_batch(CFG, mini_db, failed_path=fp)
    assert out["failed"] == 3 and out["failed_csv"]
    bl.run_batch(CFG, mini_db, failed_path=fp)          # 第二次仍失败 → attempts 累加
    rows = list(bl.csv.DictReader(Path(out["failed_csv"]).open(encoding="utf-8-sig")))
    assert {r["qid"] for r in rows} == {"1", "2", "3"}
    assert all(int(r["attempts"]) == 2 for r in rows)
    assert all(r["err_type"] == "RuntimeError" and r["model"] == CFG["model"] for r in rows)


def test_run_batch_budget_stops(mini_db, monkeypatch, tmp_path):
    """预算封顶（软上限）：超限后 cancel 未起飞的；飞行中的自然完成，总成本有界。

    F5 新口径：单题成本 = (80 in × ¥1 + 20 out × ¥4)/1e6 = 0.00016；
    budget=0.0003 → 第 2 题完成后 0.00032 超限即停。
    """
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, budget=0.0003, failed_path=tmp_path / "f.jsonl")
    assert out["labeled"] <= 3                      # 已在途请求必须收割并持久化
    assert out["all_submitted_results_consumed"] is True
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == out["labeled"]


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


def test_merge_propagates_topic_across_same_hash(mini_db, monkeypatch, tmp_path):
    """同 q_norm_hash 的拷贝 merge 后补空 topic，空拷贝顺带补齐 tags（docs/51 §5）。"""
    mini_db.execute("UPDATE questions SET q_norm_hash='duphash' WHERE qid IN (1,2)")
    mini_db.commit()
    mini_db.execute(
        "INSERT INTO labels_staging (qid, q_norm_hash, topic, qtype, difficulty, tags, provider, "
        "model, redacted, latency_ms, tokens, labeled_at) "
        "VALUES (1,'duphash','Java基础','八股',3,'[\"HashMap\"]','p','m',0,1,1,'now')")
    # 传播前 label_facts 里是同 hash 的旧值（'其他'）：传播后必须跟着改，否则重切挂回旧标签
    mini_db.execute(
        "INSERT OR REPLACE INTO label_facts (q_norm_hash, topic, qtype, difficulty, tags, "
        "normalizer_version, q_norm_sample, source, labeled_at, confidence, merged_at) "
        "VALUES ('duphash','其他','八股',3,'[\"HashMap\"]','v1','什么是 HashMap？','model','now',NULL,'now')")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert out["ok"] and out["merged"] == 1
    assert out["hash_consistency"]["topic_filled"] >= 1
    assert 2 in out["hash_consistency"]["changed_qids"]
    topic2 = mini_db.execute("SELECT topic FROM questions WHERE qid=2").fetchone()[0]
    tags2 = mini_db.execute("SELECT tags FROM questions WHERE qid=2").fetchone()[0]
    assert topic2 == "Java基础" and tags2 == '["集合"]'   # HashMap 归一成词表主键
    facts = mini_db.execute("SELECT topic, tags FROM label_facts WHERE q_norm_hash='duphash'").fetchone()
    assert facts["topic"] == "Java基础"


def test_merge_hash_consistency_does_not_overwrite_by_default(mini_db):
    """默认 fill-only：已有且不同的 topic 不被多数值覆盖；显式 overwrite 才改（P1-3）。"""
    mini_db.execute("UPDATE questions SET q_norm_hash='h2', topic=CASE qid "
                    "WHEN 1 THEN 'Java基础' WHEN 2 THEN 'Redis' WHEN 3 THEN 'Java基础' "
                    "ELSE topic END WHERE qid IN (1,2,3)")
    mini_db.execute(
        "INSERT INTO labels_staging (qid, q_norm_hash, topic, qtype, difficulty, tags, provider, "
        "model, redacted, latency_ms, tokens, labeled_at) "
        "VALUES (4,'hash4','算法','八股',3,'[\"排序\"]','p','m',0,1,1,'now')")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert out["ok"], out
    assert out["hash_consistency"]["topic_overwritten"] == 0
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=2").fetchone()[0] == "Redis"
    # 显式覆盖模式：2 份 Java基础 vs 1 份 Redis → 覆盖（内部函数直调，验证开关有效）
    res = bl._propagate_by_hash(mini_db, ["h2"], overwrite=True)
    assert res["topic_overwritten"] == 1
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=2").fetchone()[0] == "Java基础"


@pytest.mark.parametrize("order", [
    ["changed-hash", "steady-hash"],
    ["steady-hash", "changed-hash"],
])
def test_hash_propagation_only_updates_facts_for_changed_hash(mini_db, order):
    mini_db.execute(
        "UPDATE questions SET q_norm_hash='changed-hash', topic=CASE qid "
        "WHEN 1 THEN '' ELSE 'Java基础' END WHERE qid IN (1,2)")
    mini_db.execute(
        "UPDATE questions SET q_norm_hash='steady-hash', topic='MySQL' WHERE qid IN (3,4)")
    mini_db.execute(
        "INSERT INTO label_facts (q_norm_hash, topic, merged_at) VALUES ('steady-hash','旧事实','now')")
    mini_db.commit()

    out = bl._propagate_by_hash(mini_db, order)

    assert out["topic_filled"] == 1
    assert mini_db.execute(
        "SELECT topic FROM questions WHERE qid=1").fetchone()[0] == "Java基础"
    assert mini_db.execute(
        "SELECT topic FROM label_facts WHERE q_norm_hash='steady-hash'").fetchone()[0] == "旧事实"


def test_hash_propagation_without_changes_keeps_facts(mini_db):
    mini_db.execute("UPDATE questions SET q_norm_hash='steady-hash' WHERE qid IN (1,2)")
    mini_db.execute(
        "INSERT INTO label_facts (q_norm_hash, topic, merged_at) VALUES ('steady-hash','旧事实','now')")
    mini_db.commit()
    bl._propagate_by_hash(mini_db, ["steady-hash"])
    assert mini_db.execute(
        "SELECT topic FROM label_facts WHERE q_norm_hash='steady-hash'").fetchone()[0] == "旧事实"


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
    """F9+F1+T21：merge 落 topic/qtype/difficulty/tags + position/quality/canonical_q + 事实按 hash 落档。"""
    monkeypatch.setattr(bl, "label_one", _fake_label(
        {"topic": "Redis", "qtype": "八股", "difficulty": 4, "tags": ["缓存"],
         "position": ["后端"], "quality": ["残缺"], "canonical_q": "Redis 的持久化方式有哪些"}))
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    out = bl.merge(mini_db)
    assert out["ok"]
    row = mini_db.execute(
        "SELECT topic, qtype, difficulty, tags, position, quality, canonical_q "
        "FROM questions WHERE qid=1").fetchone()
    assert row["topic"] == "Redis" and row["qtype"] == "八股"
    assert row["difficulty"] == 4 and row["tags"] == '["缓存"]'
    assert row["position"] == '["后端"]' and row["quality"] == '["残缺"]'
    assert row["canonical_q"] == "Redis 的持久化方式有哪些"
    facts = mini_db.execute(
        "SELECT q_norm_hash, topic, difficulty, normalizer_version, q_norm_sample, source, "
        "labeled_at FROM label_facts").fetchall()
    assert len(facts) == 3                                  # qid 1-3 各一条（hash 互异）
    assert {r["topic"] for r in facts} == {"Redis"}
    assert {r["q_norm_hash"] for r in facts} == {"hash1", "hash2", "hash3"}
    assert {r["normalizer_version"] for r in facts} == {config.NORMALIZER_VERSION}
    assert all(r["q_norm_sample"] and r["source"] == "model" for r in facts)
    assert all(r["labeled_at"] for r in facts)


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
    assert out["ok"] and "mianjing_kb.refresh" in out["next_step"]


def test_redact_integration_in_label_one(monkeypatch):
    """label_one 的脱敏链路：题干含手机号 → redacted=1 且送 LLM 的文本已遮罩。"""
    sent = {}

    def fake_chat_json(cfg, system, user, **kw):
        sent["user"] = user
        return {"obj": {"topic": "HR", "qtype": "HR", "difficulty": 2, "tags": []},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake_chat_json)
    row = bl.label_one(CFG, {"qid": 9, "q_text": "自我介绍？电话 13812345678",  # privacy-scan: allow 合成号码
                             "a_text": "答"})
    assert row["redacted"] == 1
    assert "13812345678" not in sent["user"]                       # privacy-scan: allow
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
    archived = next(tmp_path.glob("golden_todo.loaded-*.json"))   # 载入成功后原文件已归档
    out2 = bl.golden_load(mini_db, archived)           # 默认 annotator=human（归档副本重灌）
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


def test_merge_rejects_on_g6b_fail(mini_db):
    """批次二 G3：金标抽检 <0.8 → merge 拒绝（staging 保留），提示看弱项。"""
    for i in range(50):
        _stage(mini_db, 100 + i, "JVM")                # q_norm_hash=hash{100+i}
        mini_db.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                        "VALUES (?,?,?, 'now')", (f"hash{100 + i}", "题", "数据库"))
    mini_db.commit()
    out = bl.merge(mini_db)
    assert not out["ok"] and "金标" in out["note"] and out["gate"] == "G6b"
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 50
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=1").fetchone()[0] == ""


def test_eval_golden_writes_report(mini_db):
    """批次二 G3 --eval：调 g6b_golden 评准确率 → build/label_eval.json。"""
    for i in range(50):
        _stage(mini_db, 100 + i, "JVM")
        mini_db.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                        "VALUES (?,?,?, 'now')", (f"hash{100 + i}", "题", "JVM"))
    mini_db.commit()
    g = bl.eval_golden(mini_db)
    assert g["state"] == "PASS"
    data = json.loads((config.BUILD_DIR / "label_eval.json").read_text(encoding="utf-8"))
    assert data["state"] == "PASS" and data["detail"]["topic_acc"] == 1.0


# ---- 批次三 R2：封闭词表——label_one 锁枚举 + 词表外兜底 ----

def test_label_one_vocab_lock_and_fallback(monkeypatch):
    """词表非空 → SYSTEM 锁枚举（词表注入 prompt）；词表外输出改"其他"+pending。"""
    monkeypatch.setattr(bl, "topic_vocab", lambda: ("JVM", "Redis", "其他"))
    sent = {}

    def fake_chat_json(cfg, system, user, **kw):
        sent["system"] = system
        return {"obj": {"topic": "玄学", "qtype": "八股", "difficulty": 3, "tags": []},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake_chat_json)
    row = bl.label_one(CFG, {"qid": 9, "q_norm_hash": "h9", "q_text": "题", "a_text": "答"})
    assert "词表" in sent["system"] and "JVM" in sent["system"] and "其他" in sent["system"]
    assert row["topic"] == "其他" and row["review_state"] == "pending"

    def fake2(cfg, system, user, **kw):
        return {"obj": {"topic": "JVM", "qtype": "八股", "difficulty": 3, "tags": []},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake2)
    row2 = bl.label_one(CFG, {"qid": 10, "q_norm_hash": "h10", "q_text": "题", "a_text": "答"})
    assert row2["topic"] == "JVM" and row2["review_state"] is None


def test_label_one_no_vocab_keeps_free_topic(monkeypatch):
    """词表降级（空元组）→ 旧口径：不锁枚举、不兜底，review_state=None。"""
    monkeypatch.setattr(bl, "topic_vocab", lambda: ())

    def fake(cfg, system, user, **kw):
        assert system == bl.SYSTEM
        return {"obj": {"topic": "玄学", "qtype": "八股", "difficulty": 3, "tags": []},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake)
    row = bl.label_one(CFG, {"qid": 9, "q_norm_hash": "h9", "q_text": "题", "a_text": "答"})
    assert row["topic"] == "玄学" and row["review_state"] is None


def test_label_one_new_dims_and_tag_normalization(monkeypatch):
    """T21：prompt 覆盖 position/quality/canonical_q；标签同义归一；越界岗位降级并标 pending。"""
    monkeypatch.setattr(bl, "topic_vocab", lambda: ("JVM", "Redis", "其他"))
    sent = {}

    def fake(cfg, system, user, **kw):
        sent["system"] = system
        return {"obj": {"topic": "JVM", "qtype": "八股", "difficulty": 3,
                        "position": ["后端", "火星"], "quality": ["重复"],
                        "canonical_q": "", "tags": ["JVM GC", "哈希表"]},
                "latency_ms": 5, "tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}

    monkeypatch.setattr(bl, "chat_json", fake)
    row = bl.label_one(CFG, {"qid": 11, "q_norm_hash": "h11",
                             "q_text": "什么是 JVM 的类加载？", "a_text": "答"})
    assert "canonical_q" in sent["system"] and "岗位" in sent["system"]
    assert json.loads(row["position"]) == ["后端"]          # 火星 被丢弃
    assert json.loads(row["quality"]) == ["重复"]
    assert row["review_state"] == "pending"                # 有非法值 → 待复核
    assert json.loads(row["tags"]) == ["哈希表"]           # JVM GC→JVM，但 JVM 是 topic 被剔除
    assert row["tag_norm_hits"] == 1
    assert row["canonical_q"] == "什么是 JVM 的类加载？"    # 空 → 回退题干


# ---- 批次三 R3：复核执行层（--review / --review-list / --review-resolve）----

def test_review_targets_three_classes(mini_db, monkeypatch):
    """R3 复核目标=三类：词表外（含其他）/低置信/金标弱区；已复核不再选。"""
    monkeypatch.setattr(bl, "topic_vocab", lambda: ("JVM", "Redis", "其他"))
    _stage(mini_db, 1, "其他")
    _stage(mini_db, 2, "玄学")     # A 词表外
    _stage(mini_db, 3, "JVM")      # B 低置信
    mini_db.execute("UPDATE questions SET conf='low' WHERE qid=3")
    _stage(mini_db, 4, "JVM")      # C 金标弱区（staging JVM vs gold MySQL）
    mini_db.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                    "VALUES ('hash4', '题', 'MySQL', 'now')")
    mini_db.execute("UPDATE labels_staging SET review_state='agree' WHERE qid=1")  # 已复核
    mini_db.commit()
    got = {t["qid"] for t in bl.review_targets(mini_db)}
    assert got == {2, 3, 4}


def test_run_review_updates_states(mini_db, monkeypatch, tmp_path):
    """R3：agree→agree+note；disagree→disagree+建议 JSON；失败不阻塞。"""
    _stage(mini_db, 1, "玄学")
    _stage(mini_db, 2, "怪谈")
    _stage(mini_db, 3, "乱码")
    mini_db.commit()

    def fake_review_one(cfg, row):
        if row["qid"] == 3:
            raise RuntimeError("LLM 超时")
        if row["qid"] == 1:
            return {"verdict": "agree", "suggestion": {}, "note": "主题合理",
                    "review_model": cfg["model"], "prompt_tokens": 60, "completion_tokens": 15}
        return {"verdict": "disagree",
                "suggestion": {"topic": "Redis", "qtype": "八股", "difficulty": 2},
                "note": "主题错标", "review_model": cfg["model"],
                "prompt_tokens": 60, "completion_tokens": 15}

    monkeypatch.setattr(bl, "review_one", fake_review_one)
    out = bl.run_review(CFG, mini_db, failed_path=tmp_path / "rf.jsonl")
    assert out["reviewed"] == 2 and out["failed"] == 1
    r1 = mini_db.execute("SELECT review_state, review_note FROM labels_staging WHERE qid=1").fetchone()
    assert r1[0] == "agree" and "主题合理" in r1[1]
    r2 = mini_db.execute("SELECT review_state FROM labels_staging WHERE qid=2").fetchone()
    assert r2[0] == "disagree"
    note2 = mini_db.execute("SELECT review_note FROM labels_staging WHERE qid=2").fetchone()[0]
    assert json.loads(note2)["suggestion"]["topic"] == "Redis"



def test_review_budget_stops_and_consumes_inflight(mini_db, monkeypatch, tmp_path):
    """复核达到预算后停止补单，但已在途复核均要更新 staging。"""
    import time
    for qid in (1, 2, 3):
        _stage(mini_db, qid, "玄学")
    mini_db.commit()
    cfg = {**CFG, "concurrency": 2, "price": (10.0, 10.0)}

    def fake_review(cfg_arg, row):
        time.sleep(0.02 if row["qid"] == 1 else 0.08)
        return {"verdict": "agree", "suggestion": {}, "note": "ok",
                "review_model": cfg_arg["model"], "prompt_tokens": 60,
                "completion_tokens": 15}

    monkeypatch.setattr(bl, "review_one", fake_review)
    out = bl.run_review(cfg, mini_db, budget=0.0001, failed_path=tmp_path / "rf.jsonl")
    assert out["budget_exceeded"] is True
    assert out["submitted"] == 2 and out["reviewed"] == 2
    assert out["all_submitted_results_consumed"] is True
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging WHERE review_state='agree'").fetchone()[0] == 2

def test_merge_holds_disagree_rows(mini_db, monkeypatch, tmp_path):
    """R3：merge 不落库不删 disagree 分歧行（n_review_hold 输出），终审后放行。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    mini_db.execute("UPDATE labels_staging SET review_state='disagree', review_note=? WHERE qid=1",
                    (json.dumps({"suggestion": {"topic": "Redis"}}, ensure_ascii=False),))
    mini_db.commit()
    out = bl.merge(mini_db)
    assert out["ok"] and out["merged"] == 2 and out["n_review_hold"] == 1
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=1").fetchone()[0] == ""
    left = mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    assert left == 1  # 分歧行保留在 staging


def test_run_batch_records_skipped_code_qids(mini_db, monkeypatch, tmp_path):
    """跳过碎片不能静默：skipped_code_qids 落 sidecar 供人工确认误杀。"""
    _add_question(mini_db, 5, "#include <stdio.h>")
    mini_db.commit()
    monkeypatch.setattr(bl, "label_one", _fake_label())
    out = bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    assert out["skipped_code"] == 1
    lines = (tmp_path / "label_skipped_code.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in lines if x.strip()]
    assert [r["qid"] for r in rows] == [5]
    assert rows[0]["reason"] == "code_fragment"


def test_merge_blocks_pending_rows(mini_db, monkeypatch, tmp_path):
    """pending 必须人工终审，不得被 merge 静默放行。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    mini_db.execute("UPDATE labels_staging SET review_state='pending' WHERE qid=1")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert not out["ok"] and out["gate"] == "LABEL_QUALITY"
    assert any("pending_review" in f for f in out["fails"])
    assert mini_db.execute("SELECT topic FROM questions WHERE qid=1").fetchone()[0] == ""
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 3


def test_merge_blocks_code_fragment_rows(mini_db, monkeypatch, tmp_path):
    """代码碎片进了 staging → 整批拒绝 merge，避免污染 questions.topic。"""
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    mini_db.execute("UPDATE questions SET q_text='#include <stdio.h>' WHERE qid=1")
    mini_db.commit()
    out = bl.merge(mini_db)
    assert not out["ok"] and out["gate"] == "LABEL_QUALITY"
    assert any("code_fragment" in f for f in out["fails"])


def test_review_resolve_keep_suggest_drop(mini_db):
    """R3 终审出口：keep=agree；suggest=应用建议+agree；drop=删行；未知 token 上报。"""
    _stage(mini_db, 1, "玄学")
    _stage(mini_db, 2, "怪谈")
    _stage(mini_db, 3, "乱码")
    mini_db.execute("UPDATE labels_staging SET review_state='disagree', review_note=? WHERE qid=2",
                    (json.dumps({"suggestion": {"topic": "Redis", "qtype": "八股", "difficulty": 2},
                                 "note": "错标"}, ensure_ascii=False),))
    mini_db.commit()
    out = bl.review_resolve(mini_db, "1:keep 2:suggest 3:drop 999:drop bad:keep")
    assert out["resolved"] == 3 and out["unknown"] == ["999:drop", "bad:keep"]
    assert mini_db.execute("SELECT review_state FROM labels_staging WHERE qid=1").fetchone()[0] == "agree"
    r2 = mini_db.execute("SELECT topic, qtype, difficulty, review_state FROM labels_staging WHERE qid=2").fetchone()
    assert tuple(r2) == ("Redis", "八股", 2, "agree")
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging WHERE qid=3").fetchone()[0] == 0


# ---- 批次三 R5：COALESCE 守卫（评审 Minor#1）+ 待打标口径单源（bug5）----

def test_merge_coalesce_guards_difficulty_tags(mini_db, monkeypatch, tmp_path):
    """Minor#1：staging tags 为空数组（LLM 没给）时 merge 不覆盖已有 tags；
    difficulty NULL 同守卫（G6 现拦 NULL，防御性保留）。"""
    mini_db.execute('UPDATE questions SET difficulty=4, tags=\'["旧"]\' WHERE qid=1')
    mini_db.commit()
    monkeypatch.setattr(bl, "label_one", _fake_label(obj={"tags": []}))  # difficulty 默认 3 合规
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    assert bl.merge(mini_db)["ok"]
    r = mini_db.execute("SELECT difficulty, tags FROM questions WHERE qid=1").fetchone()
    assert r[0] == 3 and json.loads(r[1]) == ["旧"]   # difficulty 正常覆盖；tags 保留旧值


def test_merge_change_counts_are_measured_before_update(mini_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "label_one", _fake_label())
    bl.run_batch(CFG, mini_db, failed_path=tmp_path / "f.jsonl")
    mini_db.execute(
        "UPDATE questions SET topic='Java基础', qtype='八股', difficulty=3, "
        "tags='[\"HashMap\"]', position='[\"后端\"]', canonical_q=q_text WHERE qid=1")
    mini_db.execute(
        "UPDATE labels_staging SET canonical_q=(SELECT q_text FROM questions WHERE qid=1) "
        "WHERE qid=1")
    mini_db.commit()

    out = bl.merge(mini_db)

    assert out["ok"] and out["merged"] == 3
    assert out["merged_changed"] == 2 and out["merged_kept_existing"] == 1


@pytest.mark.parametrize("field,before,staged,changed", [
    ("topic", None, "", 0),
    ("topic", "Java基础", "Java基础", 0),
    ("topic", "Java基础", "Redis", 1),
    ("qtype", "八股", "算法", 1),
    ("difficulty", None, None, 0),
    ("difficulty", 3, 3, 0),
    ("difficulty", 3, 4, 1),
    ("tags", None, "[]", 0),
    ("tags", '["旧"]', '["旧"]', 0),
    ("tags", '["旧"]', '["新"]', 1),
    ("position", None, None, 0),
    ("position", '["后端"]', '["后端"]', 0),
    ("position", '["后端"]', '["算法"]', 1),
    ("quality", None, None, 0),
    ("quality", '["残缺"]', '["重复"]', 1),
    ("canonical_q", None, "", 0),
    ("canonical_q", "旧问句", "新问句", 1),
])
def test_merge_change_count_matches_update_expression(mini_db, monkeypatch, field, before,
                                                       staged, changed):
    from mianjing_kb import label_quality, verify_ledger

    monkeypatch.setattr(verify_ledger, "g6_label", lambda conn: {"state": "PASS", "fails": []})
    monkeypatch.setattr(verify_ledger, "g6b_golden", lambda conn: {"state": "PASS", "detail": {}})
    monkeypatch.setattr(label_quality, "assess",
                        lambda conn: {"ok": True, "state": "PASS", "detail": {}})
    mini_db.execute(f"UPDATE questions SET {field}=? WHERE qid=1", (before,))
    values = {"topic": "", "qtype": "八股", "difficulty": None, "tags": "[]",
              "position": None, "quality": None, "canonical_q": ""}
    values[field] = staged
    mini_db.execute(
        "INSERT INTO labels_staging (qid, q_norm_hash, topic, qtype, difficulty, tags, "
        "position, quality, canonical_q, provider, model, labeled_at) "
        "VALUES (1,'hash1',?,?,?,?,?,?,?,'p','m','now')",
        (values["topic"], values["qtype"], values["difficulty"], values["tags"],
         values["position"], values["quality"], values["canonical_q"]))
    mini_db.commit()

    out = bl.merge(mini_db)

    assert out["ok"], out
    assert out["merged_changed"] == changed
    assert out["merged_kept_existing"] == 1 - changed


def test_todo_predicate_single_source(mini_db, monkeypatch):
    """bug5：待打标口径单源 TODO_PREDICATE——pick_targets 与 health 同含"未分类"。"""
    _add_question(mini_db, 9, "题九", topic="未分类")
    mini_db.commit()
    assert 9 in {r["qid"] for r in bl.pick_targets(mini_db)}   # 修复前 pick_targets 跳过未分类
    monkeypatch.setattr(bl, "chat", lambda *a, **k: {"text": "好", "latency_ms": 1})
    assert bl.health(CFG, mini_db)["todo"] == 4                # 1/2/3 空 + 9 未分类

# ---- 评审修复（2026-11-19）：Important#1/#2/#3/#4 + Minor#2/#7 ----

def test_merge_drop_orphans_spares_disagree_rows(mini_db):
    """Important#1：--drop-orphans 不得静默删分歧行——disagree 孤儿行保留待人工终审。"""
    _add_question(mini_db, 10, "题十")
    _stage(mini_db, 10, "JVM")                       # 合法行，可 merge
    mini_db.execute("INSERT OR REPLACE INTO labels_staging "
                    "(qid, q_norm_hash, topic, qtype, difficulty, tags, provider, model, "
                    "redacted, latency_ms, tokens, prompt_tokens, completion_tokens, "
                    "labeled_at, review_state) "
                    "VALUES (999, 'hash999', 'JVM', '八股', 3, '[]', 'local', 'm', "
                    "0, 1, 10, 8, 2, 'now', 'disagree')")
    mini_db.commit()
    out = bl.merge(mini_db, drop_orphans=True)
    assert out["ok"] and out["merged"] == 1 and out["n_review_hold"] == 1
    r = mini_db.execute("SELECT review_state FROM labels_staging WHERE qid=999").fetchone()
    assert r and r[0] == "disagree"                  # 分歧行未被孤儿清理误删


def test_review_list_shows_orphaned_disagree_rows(mini_db):
    """Important#1b：review_list 改 LEFT JOIN——题干已失效的分歧行仍可见（可 drop 终审）。"""
    mini_db.execute("INSERT OR REPLACE INTO labels_staging "
                    "(qid, q_norm_hash, topic, qtype, difficulty, tags, provider, model, "
                    "redacted, latency_ms, tokens, prompt_tokens, completion_tokens, "
                    "labeled_at, review_state) "
                    "VALUES (999, 'hash999', 'JVM', '八股', 3, '[]', 'local', 'm', "
                    "0, 1, 10, 8, 2, 'now', 'disagree')")
    mini_db.commit()
    rows = bl.review_list(mini_db)
    assert len(rows) == 1 and rows[0]["qid"] == 999
    assert "(题干已失效)" in (rows[0]["q_text"] or "")


def test_review_list_prints_suggestion(mini_db, monkeypatch, capsys):
    """Important#3：终审清单打印建议（review_note 摘要）——人工有判断依据。"""
    monkeypatch.setattr(bl, "resolve_config", lambda: CFG)   # main() 走 main 的配置解析
    _stage(mini_db, 1, "JVM")
    mini_db.execute("UPDATE labels_staging SET review_state='disagree', review_note=? "
                    "WHERE qid=1",
                    (json.dumps({"suggestion": {"topic": "Redis"}, "note": "主题判错"},
                                ensure_ascii=False),))
    mini_db.commit()
    rc = bl.main(["--review-list", "--db", str(config.DB_PATH)])
    outp = capsys.readouterr().out
    assert rc == 0 and "建议" in outp and "主题判错" in outp


def test_review_targets_gold_mismatch_rows_selected(mini_db, monkeypatch):
    """Important#2：金标弱区改按行判定——金标判错的行必进复核。

    旧实现按预测主题聚合（HAVING<0.8），错行混进高准确率预测组时漏检
    （如 Redis 组 4/5=0.8，5 号错行不进复核）；新实现按 q_norm_hash 对照。
    """
    monkeypatch.setattr(bl, "topic_vocab", lambda: ("JVM", "Redis", "其他"))
    for qid in range(5, 10):
        _add_question(mini_db, qid, f"题{qid}")
        _stage(mini_db, qid, "Redis")
    gold = {"hash5": "JVM", "hash6": "Redis", "hash7": "Redis",
            "hash8": "Redis", "hash9": "Redis"}      # Redis 组 4/5=0.8：旧口径漏检
    for h, t in gold.items():
        mini_db.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                        "VALUES (?,?,?,'now')", (h, "题", t))
    mini_db.commit()
    qids = {r["qid"] for r in bl.review_targets(mini_db)}
    assert 5 in qids                                 # 金标判错必进复核
    assert not ({6, 7, 8, 9} & qids)                 # 金标认可且在词表内不重选


def test_golden_sample_refuses_overwrite_of_pending_todo(mini_db):
    """Important#4：已有未回灌 golden_todo.json → 拒绝重抽（防覆盖人工已填内容）。"""
    _stage(mini_db, 1, "JVM")
    p = config.BUILD_DIR / "golden_todo.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sampled": 1, "items": [
        {"q_norm_hash": "hash1", "topic_gold": "已填"}]}, ensure_ascii=False),
        encoding="utf-8")
    out = bl.golden_sample(mini_db, 3)
    assert not out["ok"] and "已存在" in out["note"]
    assert "已填" in p.read_text(encoding="utf-8")   # 原文件未被覆盖
    p.unlink()                                       # 清掉后可正常重抽
    out2 = bl.golden_sample(mini_db, 1)
    assert out2["sampled"] == 1


def test_golden_load_archives_todo_file(mini_db, tmp_path):
    """Important#4：--golden-load 成功后 todo 文件改名留档（loaded-<ts>）→ 可自由重抽。"""
    _stage(mini_db, 1, "JVM")
    todo = {"items": [{"q_norm_hash": "hash1", "topic_gold": "JVM"}]}
    p = tmp_path / "golden_todo.json"
    p.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
    assert bl.golden_load(mini_db, p)["ok"]
    assert not p.exists()
    archived = list(tmp_path.glob("golden_todo.loaded-*.json"))
    assert len(archived) == 1


def test_golden_load_rejects_missing_or_malformed_file(mini_db, tmp_path):
    """Minor#2：文件缺失/JSON 坏/顶层非对象 → 优雅拒绝返回 {"ok": False}，不裸抛。"""
    out = bl.golden_load(mini_db, tmp_path / "nope.json")
    assert not out["ok"] and out["errors"]
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    out2 = bl.golden_load(mini_db, bad)
    assert not out2["ok"] and out2["errors"]
    lst = tmp_path / "list.json"
    lst.write_text("[1,2]", encoding="utf-8")
    out3 = bl.golden_load(mini_db, lst)
    assert not out3["ok"] and out3["errors"]


def test_read_only_branch_ignores_kb_write_lock(mini_db, monkeypatch, capsys):
    """D-28：--check 只读不应被 intake 的写锁挡住，也不应触发迁移。"""
    from mianjing_kb.kb import wlock
    monkeypatch.setattr(bl, "resolve_config", lambda: CFG)
    monkeypatch.setattr(bl, "health", lambda cfg, conn: {"ok": True})
    with wlock.write_lock("kb", tool="intake", build_dir=config.BUILD_DIR,
                          target=str(config.DB_PATH)):
        rc = bl.main(["--check", "--db", str(config.DB_PATH)])
    assert rc == 0
    assert "ok" in capsys.readouterr().out


def test_read_only_branch_never_creates_missing_db(tmp_path, monkeypatch):
    """D-28：只读分支遇到缺失库必须报错返回 2，不能 sqlite3 静默建空库。"""
    missing = tmp_path / "missing.sqlite"
    rc = bl.main(["--manual-template", "1", "--db", str(missing)])
    assert rc == 2
    assert not missing.exists()


def test_budget_stop_consumes_and_persists_all_inflight(mini_db, monkeypatch, tmp_path):
    """超预算后不再提交新任务，但所有已提交 Future 必须被消费并持久化。"""
    import time
    cfg = {**CFG, "concurrency": 2, "price": (10.0, 10.0)}
    _add_question(mini_db, 5, "第五个待打标题？")
    _add_question(mini_db, 6, "第六个待打标题？")
    mini_db.commit()

    def fake(cfg_arg, q):
        if q["qid"] == 1:
            time.sleep(0.02)
            return _fake_label()(cfg_arg, q)
        if q["qid"] == 2:
            time.sleep(0.08)
            return _fake_label()(cfg_arg, q)
        raise AssertionError("预算触发后不应提交 qid>=3")

    monkeypatch.setattr(bl, "label_one", fake)
    out = bl.run_batch(cfg, mini_db, budget=0.0001, failed_path=tmp_path / "f.jsonl")
    assert out["budget_exceeded"] is True, out
    assert out["submitted"] == 2
    assert out["labeled"] == 2
    assert out["all_submitted_results_consumed"] is True
    assert mini_db.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0] == 2


def test_manual_template_escapes_formula_cells(mini_db, tmp_path):
    import csv
    mini_db.execute("UPDATE questions SET q_text=? WHERE qid=1", (' =HYPERLINK("https://evil.invalid","x")',))
    mini_db.execute("UPDATE questions SET a_text=? WHERE qid=2", ("+cmd|' /C calc'!A0",))
    mini_db.commit()
    path = tmp_path / "manual.csv"
    bl.manual_template(mini_db, 3, path, seed=1)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    q1 = next(r for r in rows if r["qid"] == "1")
    q2 = next(r for r in rows if r["qid"] == "2")
    assert q1["q_text"].startswith("' ")
    assert q2["a_text"].startswith("'+cmd")
