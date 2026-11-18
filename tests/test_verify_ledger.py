# -*- coding: utf-8 -*-
"""verify_ledger 门禁测试（docs/15 §2.2 验收：≥6 条 + 人为故障各自报警）。

覆盖：G1 全覆盖/FAIL/清单/skip_log 豁免、G2 转换链三段（含 350道 ocr_text 回归样本）、
G3 空串口径、G4 vec 缺失 WARN / 不一致 FAIL、G5 台账-磁盘、exit code、G7 文档一致性。
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import verify_ledger as vl  # noqa: E402
from tools.kb import config  # noqa: E402

REAL_META = config.META_DIR  # patch 前抓真 schema 路径

MD_A = "题库/综合/甲.md"
MD_B = "题库/综合/乙.md"

OUT_SCHEMA = """
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER,
  q_text TEXT, a_text TEXT, qtype TEXT, conf TEXT, q_norm_hash TEXT, topic TEXT, category TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT,
  mirror_hash TEXT, content TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text);
"""


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    """迷你双库 + md 树 + 99 区：默认 G1-G5 全 PASS（G4 因无 vec 表为 WARN）。"""
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    md_dir = corpus / "20-题库MD" / "题库" / "综合"
    src_dir = corpus / "02-Java进阶"
    q99 = corpus / "99-重复文件待删"
    for d in (build, md_dir, src_dir, q99, tmp_path / "meta"):
        d.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(config, "QUESTION_MD_DIR", corpus / "20-题库MD")
    monkeypatch.setattr(config, "META_DIR", tmp_path / "meta")

    (src_dir / "甲.pdf").write_bytes(b"jia")
    (src_dir / "乙.pdf").write_bytes(b"yi")
    (q99 / "丙.pdf").write_bytes(b"bing")            # 99 区件，内容 hash 进台账
    (md_dir / "甲.md").write_text("# 甲\n\n什么是 JVM？", encoding="utf-8")
    (md_dir / "乙.md").write_text("# 乙\n\n什么是 MySQL？", encoding="utf-8")

    kb = sqlite3.connect(build / "kb.sqlite")
    kb.row_factory = sqlite3.Row
    kb.executescript((REAL_META / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    kb.executescript((REAL_META / "schema" / "004_questions.sql").read_text(encoding="utf-8"))
    kb.executescript((REAL_META / "schema" / "005_labels_staging.sql").read_text(encoding="utf-8"))
    if not any(r[1] == "topic" for r in kb.execute("PRAGMA table_info(questions)")):
        kb.execute("ALTER TABLE questions ADD COLUMN topic TEXT")   # 006 题级 topic（db.py 同款）
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)",
               (_h(b"jia"), _h(b"jia") + "0" * 48, "甲.pdf", ".pdf", 3, str(src_dir / "甲.pdf"),
                "02-Java进阶", "classified", "t", "now", "now", None))
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)",
               (_h(b"yi"), _h(b"yi") + "0" * 48, "乙.pdf", ".pdf", 2, str(src_dir / "乙.pdf"),
                "02-Java进阶", "classified", "t", "now", "now", None))
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)",
               (_h(b"gone"), _h(b"gone") + "0" * 48, "丙.pdf", ".pdf", 4,
                str(corpus / "03" / "丙.pdf"), "03-数据库", "superseded", "t", "now", "now",
                "converted_to:丙.docx"))                       # 已删 + 留痕 → G5 过
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)",
               (_h(b"bing"), _h(b"bing") + "0" * 48, "丙.pdf", ".pdf", 4, str(q99 / "丙.pdf"),
                "99-重复文件待删", "quarantined", "t", "now", "now", None))
    kb.commit()

    out = sqlite3.connect(build / "面经库.db")
    out.row_factory = sqlite3.Row
    out.executescript(OUT_SCHEMA)
    out.execute("INSERT INTO doc_contents (md_rel, doc_hash, mirror_hash, content) VALUES (?,?,?,?)",
                (MD_A, _h(b"jia"), vl.mirror_hash_of(MD_A), "# 甲"))
    out.execute("INSERT INTO doc_contents (md_rel, doc_hash, mirror_hash, content) VALUES (?,?,?,?)",
                (MD_B, _h(b"yi"), vl.mirror_hash_of(MD_B), "# 乙"))
    # 甲走镜像 hash（extract 落库口径），乙走源 hash 直连
    out.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic) "
                "VALUES (1, ?, 1, '什么是 JVM？', 'x', 'JVM')", (vl.mirror_hash_of(MD_A),))
    out.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic) "
                "VALUES (2, ?, 1, '什么是 MySQL？', 'y', 'MySQL')", (_h(b"yi"),))
    out.execute("INSERT INTO questions_fts (rowid, q_text, a_text) VALUES (1, '什么是 JVM？', '')")
    out.execute("INSERT INTO questions_fts (rowid, q_text, a_text) VALUES (2, '什么是 MySQL？', '')")
    out.commit()

    yield {"corpus": corpus, "build": build, "kb": kb, "out": out,
           "hA": _h(b"jia"), "hB": _h(b"yi")}
    kb.close()
    out.close()


# ---------------- G1 ----------------
def test_g1_pass(mini_env):
    g = vl.g1_coverage(mini_env["kb"], mini_env["out"], {})
    assert g["state"] == "PASS" and g["fails"] == []
    assert g["detail"]["classified"] == 2


def test_g1_fail_and_backfill_plan(mini_env):
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")          # 乙：有 doc 无题
    out.commit()
    g = vl.g1_coverage(mini_env["kb"], out, {})
    assert g["state"] == "FAIL"
    f = [x for x in g["fails"] if x["rel"] == "02-Java进阶/乙.pdf"]
    assert f and f[0]["has_doc_contents"] == "Y" and f[0]["suggest"] == "extract 重跑"


def test_g1_skip_log_exempts(mini_env):
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")
    out.commit()
    skip = {"02-Java进阶/乙.pdf": "garbled"}
    g = vl.g1_coverage(mini_env["kb"], out, skip)
    assert g["state"] == "PASS"
    assert g["detail"]["skipped_by_log"] == 1


# ---------------- G2 ----------------
def test_g2_ocr_text_without_md(mini_env):
    """350道 回归样本：OCR 全文在、md 无 → G2 必须报。"""
    ocr = mini_env["build"] / "ocr_text" / "02-Java进阶"
    ocr.mkdir(parents=True)
    (ocr / "戊.pdf.txt").write_text("===== page 1 =====\nOCR 全文", encoding="utf-8")
    g = vl.g2_chain(mini_env["out"], mini_env["corpus"] / "20-题库MD",
                    mini_env["build"] / "ocr_text", {})
    assert g["state"] == "FAIL"
    assert any(x["kind"] == "ocr_text_no_md" and "戊" in x["rel"] for x in g["fails"])


def test_g2_zero_questions(mini_env):
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")
    out.commit()
    g = vl.g2_chain(out, mini_env["corpus"] / "20-题库MD",
                    mini_env["build"] / "ocr_text", {})
    assert any(x["kind"] == "md_zero_questions" and x["rel"] == MD_B for x in g["fails"])
    # 标 no_question 后豁免
    g2 = vl.g2_chain(out, mini_env["corpus"] / "20-题库MD",
                     mini_env["build"] / "ocr_text", {MD_B: "no_question"})
    assert not any(x.get("rel") == MD_B for x in g2["fails"])


# ---------------- G3 ----------------
def test_g3_empty_topic(mini_env):
    out = mini_env["out"]
    assert vl.g3_topic(out)["state"] == "PASS"
    out.execute("UPDATE questions SET topic='' WHERE qid=1")
    out.commit()
    g = vl.g3_topic(out)
    assert g["state"] == "FAIL" and g["detail"]["empty"] == 1


# ---------------- G4 ----------------
def test_g4_vec_missing_warns(mini_env):
    g = vl.g4_index(mini_env["out"])
    assert g["state"] == "WARN"          # 向量层可选：未建不阻断


def test_g4_vec_mismatch_fails(mini_env):
    out = mini_env["out"]
    out.execute("CREATE TABLE vec_questions(rowid INTEGER PRIMARY KEY)")
    out.execute("INSERT INTO vec_questions (rowid) VALUES (1)")   # 缺 qid=2
    out.commit()
    g = vl.g4_index(out)
    assert g["state"] == "FAIL"
    assert any(x["kind"] == "vec_mismatch" for x in g["fails"])


# ---------------- G5 ----------------
def test_g5_src_missing_and_orphan_99(mini_env):
    kb = mini_env["kb"]
    kb.execute("UPDATE source_documents SET src_path='不存在.pdf' WHERE file_name='甲.pdf'")
    kb.commit()
    (mini_env["corpus"] / "99-重复文件待删" / "孤儿.pdf").write_bytes(b"orphan")
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    assert g["state"] == "FAIL"
    kinds = {x["kind"] for x in g["fails"]}
    assert "classified_src_missing" in kinds and "orphan_in_99" in kinds


def test_g5_converted_to_exempts(mini_env):
    """superseded 在正式区 + note 有留痕（converted_to / review_cli 退役）→ 合法；
    note 清空 → FAIL（真孤儿：标退役却无任何记录）。"""
    kb = mini_env["kb"]
    p = mini_env["corpus"] / "02-Java进阶" / "丙.pdf"
    p.write_bytes(b"bing2")
    kb.execute("UPDATE source_documents SET src_path=? WHERE file_name='丙.pdf'", (str(p),))
    kb.commit()
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    assert not any(x["kind"] == "superseded_not_quarantined" for x in g["fails"])
    kb.execute("UPDATE source_documents SET note=NULL WHERE file_name='丙.pdf'")
    kb.commit()
    g2 = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    assert any(x["kind"] == "superseded_not_quarantined" for x in g2["fails"])


def test_g5_retire_log_exempts_mirror(mini_env):
    """退役 md 镜像进 99 区：台账不登记派生件，retire_log 有记录即合法（v1.0/v2.0 口径）。"""
    q99 = mini_env["corpus"] / "99-重复文件待删"
    (q99 / "旧镜像.md").write_text("x", encoding="utf-8")
    rl = mini_env["build"] / "retire_log.csv"
    rl.write_text("t1,retire 旧镜像,旧镜像.md,99-重复文件待删\\旧镜像.md",
                  encoding="utf-8-sig")
    g = vl.g5_disk(mini_env["kb"], mini_env["corpus"], mini_env["build"])
    assert not any(x["kind"] == "orphan_in_99" for x in g["fails"])


# ---------------- G6 ----------------
def test_g6_skip_and_pass_and_fail(mini_env):
    kb = mini_env["kb"]
    # staging 空 → SKIP
    g = vl.g6_label(kb)
    assert g["state"] == "SKIP"
    # 合格打标 → PASS（先给 kb.questions 插基线 topic 行，防漂移软指标误触发）
    kb.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
               "q_norm_hash, simhash, span_start, span_end, topic, extracted_at) VALUES "
               "(1,'h1',1,'什么是JVM？','','八股','high','x','y',0,0,1,'JVM','now')")
    kb.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
               "model, redacted, latency_ms, tokens, labeled_at) VALUES "
               "(1,'JVM','八股',3,'[\"类加载\"]','p','m',0,1,1,'now')")
    kb.commit()
    g2 = vl.g6_label(kb)
    assert g2["state"] == "PASS" and g2["detail"]["ok_rate"] == 1.0
    # schema 不合格（qtype 越枚举 + difficulty 越界 + tags 非 JSON）→ FAIL
    kb.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
               "model, redacted, latency_ms, tokens, labeled_at) VALUES "
               "(2,'','玄学',9,'oops','p','m',0,1,1,'now')")
    kb.commit()
    g3 = vl.g6_label(kb)
    assert g3["state"] == "FAIL"
    assert g3["detail"]["ok_rate"] == 0.5          # 1/2 合格 < 0.95
    assert any(f.get("kind") == "schema_ok_rate" for f in g3["fails"])


def test_g6_first_batch_no_drift_warn(mini_env):
    """F8/bug1：首批打标（questions 现有 topic 全空=无基线）不得因 drift 误报 WARN。

    原实现 drift 基线=questions 现有非空 topic，首批打标前必空 → 所有 topic 都算
    "新主题" → drift=1.0 必 WARN。无基线时漂移指标无意义，应跳过并留痕。
    """
    kb = mini_env["kb"]
    kb.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
               "q_norm_hash, simhash, span_start, span_end, topic, extracted_at) VALUES "
               "(1,'h1',1,'什么是JVM？','','八股','high','x','y',0,0,1,'','now')")
    kb.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
               "model, redacted, latency_ms, tokens, labeled_at) VALUES "
               "(1,'JVM','八股',3,'[\"类加载\"]','p','m',0,1,1,'now')")
    kb.commit()
    g = vl.g6_label(kb)
    assert g["state"] == "PASS"
    assert g["detail"]["new_topic_ratio"] == 0.0
    assert g["detail"].get("drift_baseline") == "none(首批)"


# ---------------- main / exit code ----------------
def test_main_exit_codes(mini_env, capsys):
    assert vl.main([]) == 0                       # 干净环境（G4 WARN 不阻断）
    capsys.readouterr()                            # 清第一次输出，避免两段 JSON 叠加
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")
    out.commit()
    assert vl.main([]) == 1                       # 硬门禁 FAIL → 非 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verdict"] == "FAIL" and "G1" in summary["hard_fail"]
    # 门禁即清单：backfill_plan.csv 已产出
    plan = (mini_env["build"] / "backfill_plan.csv").read_text(encoding="utf-8-sig")
    assert "02-Java进阶/乙.pdf" in plan


# ---------------- G7 ----------------
def test_g7_docs_warns(tmp_path):
    (tmp_path / "README.md").write_text(
        "# t\n\n- tests/                    36 个用例\n\n"
        "- docs/  方案与决策（00 决策记录 ~ 16 复核）\n\n"
        "## 尚未实现\n\n- `verify_ledger.py`（G1-G7 门禁）未落地；\n\n## 下一节\n",
        encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "verify_ledger.py").write_text("", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "17-审核.md").write_text("x", encoding="utf-8")
    g = vl.g7_docs(tmp_path, actual_tests=99)
    assert g["state"] == "WARN"
    kinds = {x["kind"] for x in g["fails"]}
    assert kinds == {"tests_count_stale", "unimplemented_listed_but_exists", "docs_index_stale"}
