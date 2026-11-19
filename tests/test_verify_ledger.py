# -*- coding: utf-8 -*-
"""verify_ledger 门禁测试（docs/15 §2.2 验收：≥6 条 + 人为故障各自报警）。

覆盖：G1 全覆盖/FAIL/清单/skip_log 豁免、G2 转换链三段（含 350道 ocr_text 回归样本）、
G3 空串口径、G4 vec 缺失 WARN / 不一致 FAIL、G5 台账-磁盘、exit code、G7 文档一致性。
"""
import hashlib
import json
import sqlite3

import pytest
from mianjing_kb import verify_ledger as vl  # noqa: E402
from mianjing_kb.kb import config, db  # noqa: E402

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
    # 012（T21）：staging 新维度列（mini 库不走 db.connect，手工补）
    for col, decl in (("position", "TEXT"), ("quality", "TEXT"), ("canonical_q", "TEXT"),
                      ("tag_norm_hits", "INTEGER")):
        if not any(r[1] == col for r in kb.execute("PRAGMA table_info(labels_staging)")):
            kb.execute(f"ALTER TABLE labels_staging ADD COLUMN {col} {decl}")
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
    # 历史裸格式的接替件（T19）：converted_to 只写文件名，按名字解析到这条 rejected 记录
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?,?)",
               (_h(b"bing-docx"), _h(b"bing-docx") + "0" * 48, "丙.docx", ".docx", 5,
                str(corpus / "03" / "丙.docx"), "03-数据库", "rejected", "t", "now", "now", None))
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
    # P0-1 单身份：md 产物题行也用源 doc_hash（不再挂 md-mirror 身份）
    out.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic) "
                "VALUES (1, ?, 1, '什么是 JVM？', 'x', 'JVM')", (_h(b"jia"),))
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


def test_g4_vec_metadata_pass_and_mismatch():
    """T1：vec0 带 topic/qtype/difficulty 且逐行一致才 PASS；旧表/值不一致都 FAIL。"""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from mianjing_kb import embed_questions as E

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, topic TEXT, qtype TEXT, difficulty INTEGER);"
        "CREATE VIRTUAL TABLE questions_fts USING fts5(q_text);"
        "INSERT INTO questions VALUES (1,'JVM','八股',3),(2,'MySQL','八股',2);"
        "INSERT INTO questions_fts(rowid, q_text) VALUES (1,'jvm'),(2,'mysql');")
    E.create_vec_table(con)
    for qid, topic, diff in ((1, "JVM", 3), (2, "MySQL", 2)):
        con.execute("INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty) "
                    "VALUES (?,?,?,?,?)",
                    (qid, sqlite_vec.serialize_float32([0.0] * E.DIM), topic, "八股", diff))
    con.commit()
    assert vl.g4_index(con)["state"] == "PASS"
    con.execute("UPDATE vec_questions SET topic='Redis' WHERE rowid=1")
    con.commit()
    g = vl.g4_index(con)
    assert g["state"] == "FAIL"
    assert any(x["kind"] == "vec_metadata_mismatch" for x in g["fails"])
    con.close()


# ---------------- G8 单身份（P0-1） ----------------
def test_g8_pass_when_single_identity(mini_env):
    g = vl.g8_single_identity(mini_env["out"])
    assert g["id"] == "G8" and g["state"] == "PASS"
    assert g["detail"]["bad_products"] == 0


def test_g8_fails_when_mirror_rows_survive(mini_env):
    """有源回链的产物若还挂着 md-mirror 题行 → 同一原文抽了两遍，必须 FAIL。"""
    out = mini_env["out"]
    out.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic) "
                "VALUES (3, ?, 1, '什么是 JVM？', 'x', 'JVM')", (vl.mirror_hash_of(MD_A),))
    out.commit()
    g = vl.g8_single_identity(out)
    assert g["state"] == "FAIL"
    assert g["fails"][0]["md"] == MD_A and g["fails"][0]["mirror_rows"] == 1


def test_g8_ignores_orphan_md(mini_env):
    """没有源文档的孤儿 md（doc_hash IS NULL）保留自身身份，不属于双份。"""
    out = mini_env["out"]
    rel = "题库/综合/孤儿.md"
    out.execute("INSERT INTO doc_contents (md_rel, doc_hash, mirror_hash, content) VALUES (?,?,?,?)",
                (rel, None, vl.mirror_hash_of(rel), "# 孤儿"))
    out.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic) "
                "VALUES (4, ?, 1, '孤儿题', 'z', '综合')", (vl.mirror_hash_of(rel),))
    out.commit()
    assert vl.g8_single_identity(out)["state"] == "PASS"


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


def test_g6_extended_dims_coverage_and_validation(mini_env):
    """T21：position/quality/canonical_q 有值时校验枚举与长度；覆盖率进 detail，不误伤历史行。"""
    kb = mini_env["kb"]
    for qid in range(1, 21):
        kb.execute(
            "INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, position, quality, "
            "canonical_q, tag_norm_hits, provider, model, redacted, latency_ms, tokens, labeled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, "JVM", "八股", 3, '["类加载"]', '["后端"]', '["重复"]',
             "JVM 类加载过程是什么", 1 if qid <= 5 else 0, "p", "m", 0, 1, 1, "now"))
    kb.commit()
    g = vl.g6_label(kb)
    assert g["state"] == "PASS"
    cov = g["detail"]["coverage"]
    assert cov["canonical_q"] == 1.0 and cov["position"] == 1.0 and cov["quality"] == 1.0
    assert g["detail"]["tag_norm_hits"] == 5

    # 2/20 违法 → ok_rate 0.9 < 0.95 FAIL，且 bad_fields 标明具体维度
    kb.execute("UPDATE labels_staging SET position='[\"火星\"]' WHERE qid=1")
    kb.execute("UPDATE labels_staging SET canonical_q=? WHERE qid=2", ("长" * 70,))
    kb.commit()
    g2 = vl.g6_label(kb)
    assert g2["state"] == "FAIL"
    fields = {f for row in g2["fails"] if "bad_fields" in row for f in row["bad_fields"]}
    assert {"position", "canonical_q"} <= fields


def test_g6_first_batch_no_drift_warn(mini_env, monkeypatch):
    """F8/bug1：首批打标（questions 现有 topic 全空=无基线）不得因 drift 误报 WARN。

    原实现 drift 基线=questions 现有非空 topic，首批打标前必空 → 所有 topic 都算
    "新主题" → drift=1.0 必 WARN。无基线时漂移指标无意义，应跳过并留痕。
    批次三 R2 后该分支仅在词表不可用（topic_vocab 空）时可达——monkeypatch 模拟。
    """
    monkeypatch.setattr(vl, "topic_vocab", lambda: ())
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


def test_g6_drift_vocab_baseline(mini_env, monkeypatch):
    """批次三 R2：词表非空 → drift 基线=词表——词表外主题算漂移（捕捉 prompt 锁失败）。"""
    monkeypatch.setattr(vl, "topic_vocab", lambda: ("JVM", "Redis", "其他"))
    kb = mini_env["kb"]
    kb.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
               "model, redacted, latency_ms, tokens, labeled_at) VALUES "
               "(1,'JVM','八股',3,'[]','p','m',0,1,1,'now'),"
               "(2,'玄学','八股',3,'[]','p','m',0,1,1,'now'),"
               "(3,'怪谈','八股',3,'[]','p','m',0,1,1,'now')")
    kb.commit()
    g = vl.g6_label(kb)
    assert g["detail"]["drift_baseline"] == "vocab"
    assert g["state"] == "WARN"                    # 2/3 词表外 > 0.15
    # 全部落回词表内 → 漂移归零
    kb.execute("UPDATE labels_staging SET topic='Redis' WHERE qid IN (2,3)")
    kb.commit()
    g2 = vl.g6_label(kb)
    assert g2["state"] == "PASS" and g2["detail"]["new_topic_ratio"] == 0.0


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
    (tmp_path / "mianjing_kb").mkdir()
    (tmp_path / "mianjing_kb" / "verify_ledger.py").write_text("", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "17-审核.md").write_text("x", encoding="utf-8")
    g = vl.g7_docs(tmp_path, actual_tests=99)
    assert g["state"] == "WARN"
    kinds = {x["kind"] for x in g["fails"]}
    assert kinds == {"tests_count_stale", "unimplemented_listed_but_exists", "docs_index_stale"}


# ---- 批次二 G3：G6b 金标抽检门禁（docs/15 G6 规格"抽检准确率 ≥0.8"自动化）----

def _golden_db(tmp_path):
    from mianjing_kb.kb import db as kbdb
    return kbdb.connect(tmp_path / "kb.sqlite")     # 全迁移：labels_staging + golden_labels


def _pair(conn, i: int, gold: str = "JVM", pred=None):
    """插一对金标 + staging 行（q_norm_hash=gh{i}）；pred=None 表示标注与金标一致。"""
    h = f"gh{i}"
    conn.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                 "VALUES (?,?,?, 'now')", (h, f"题{i}", gold))
    conn.execute("INSERT INTO labels_staging(qid, q_norm_hash, topic, qtype, difficulty, "
                 "tags, provider, model, redacted, latency_ms, tokens, labeled_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'now')",
                 (1000 + i, h, gold if pred is None else pred,
                  "八股", 3, "[]", "local", "m", 0, 1, 10))


def test_g6b_skip_no_golden(tmp_path):
    conn = _golden_db(tmp_path)
    g = vl.g6b_golden(conn)
    assert g["state"] == "SKIP"
    conn.close()


def test_g6b_warn_small_sample(tmp_path):
    """matched < 20 → WARN（样本不足，未判；不挡 merge，但不再静默 SKIP）。"""
    conn = _golden_db(tmp_path)
    for i in range(5):
        _pair(conn, i)
    conn.commit()
    g = vl.g6b_golden(conn)
    assert g["state"] == "WARN" and g["detail"]["matched"] == 5
    assert "样本不足" in g["detail"]["note"]
    conn.close()


def test_g6b_warn_no_overlap(tmp_path):
    """金标与 staging 零交集也是 WARN，不能读成"通过"。"""
    conn = _golden_db(tmp_path)
    for i in range(25):
        conn.execute("INSERT INTO golden_labels(q_norm_hash, q_text, topic_gold, created_at) "
                     "VALUES (?,?,?, 'now')", (f"only{i}", f"题{i}", "JVM"))
    conn.commit()
    g = vl.g6b_golden(conn)
    assert g["state"] == "WARN" and g["detail"]["matched"] == 0
    conn.close()


def test_g6b_pass_and_fail(tmp_path):
    """50 matched：全对 PASS；12 题标错 → 0.76 FAIL + weak_topics + 混淆矩阵。"""
    conn = _golden_db(tmp_path)
    for i in range(50):
        _pair(conn, i)
    conn.commit()
    g = vl.g6b_golden(conn)
    assert g["state"] == "PASS" and g["detail"]["topic_acc"] == 1.0
    assert g["detail"]["weak_topics"] == []
    for i in range(12):
        conn.execute("UPDATE labels_staging SET topic='数据库' WHERE qid=?", (1000 + i,))
    conn.commit()
    g2 = vl.g6b_golden(conn)
    assert g2["state"] == "FAIL" and g2["detail"]["topic_acc"] == 0.76
    assert g2["detail"]["weak_topics"] == ["JVM"]
    assert g2["detail"]["matrix"]["JVM"] == {"数据库": 12, "JVM": 38}
    conn.close()


def test_g5_legacy_bare_same_name_multiple_candidates_is_ambiguous(mini_env):
    """裸 converted_to:同名.docx 若在库里有多条同名记录，不能挑一条合格的就算 PASS。"""
    kb = mini_env["kb"]
    for d, st in (("08-前端", "classified"), ("09-其他", "classified")):
        (mini_env["corpus"] / d).mkdir(parents=True, exist_ok=True)
        (mini_env["corpus"] / d / "同名.docx").write_bytes(b"x")
        kb.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"dup{d[:2]}".ljust(16, "0"), "0" * 64, "同名.docx", ".docx", 1,
             str(mini_env["corpus"] / d / "同名.docx"), d, st, "t", "now", "now", None))
    kb.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("eeeeeeeeeeeeeeee", "0" * 64, "己.doc", ".doc", 4,
         str(mini_env["corpus"] / "03" / "己.doc"), "03-数据库", "superseded", "t", "now", "now",
         "converted_to:同名.docx"))
    kb.commit()
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    fails = [f for f in g["fails"] if f["kind"] == "converted_target_missing"]
    assert g["state"] == "FAIL"
    assert any(f.get("why") == "ambiguous" for f in fails)


def test_g5_new_converted_note_requires_target_record(mini_env):
    """新格式 converted_to:<dir>/<name>#<hash> 必须能解析到一条未退役的后继记录。"""
    kb = mini_env["kb"]
    kb.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("deadbeefdeadbeef", "0" * 64, "丁.doc", ".doc", 4, str(mini_env["corpus"] / "03" / "丁.doc"),
         "03-数据库", "superseded", "t", "now", "now",
         "converted_to:03-数据库/丁.docx#abcd1234 | needs_convert"))
    kb.commit()
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    assert g["state"] == "FAIL"
    assert any(f["kind"] == "converted_target_missing" for f in g["fails"])



# ---------------- T19：历史裸 converted_to ----------------
def test_g5_legacy_bare_converted_to_without_target_fails(mini_env):
    """历史裸格式 converted_to:丙.docx 不再被跳过：找不到后继记录必须 FAIL。"""
    kb = mini_env["kb"]
    kb.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("feedfacefeedface", "0" * 64, "戊.doc", ".doc", 4,
         str(mini_env["corpus"] / "03" / "戊.doc"), "03-数据库", "superseded", "t", "now", "now",
         "converted_to:根本没有这个.docx"))
    kb.commit()
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    fails = [f for f in g["fails"] if f["kind"] == "converted_target_missing"]
    assert g["state"] == "FAIL"
    assert fails and fails[0]["legacy"] is True
    assert fails[0]["target"]["name"] == "根本没有这个.docx"
    assert g["detail"]["legacy_converted_checked"] >= 2      # 夹具里的丙 + 本测试的戊


def test_audit_converted_reports_unresolved_and_legacy(mini_env):
    """`--audit-converted` 的底层盘点：逐行给出 targets / resolved / legacy。"""
    kb = mini_env["kb"]
    kb.execute(
        "UPDATE source_documents SET note='converted_to:也没有这个.docx' "
        "WHERE file_name='丙.pdf' AND status='superseded'")
    kb.commit()
    rows = vl.audit_converted(kb)
    bad = [r for r in rows if not r["resolved"]]
    assert bad and bad[0]["rel"] == "03-数据库/丙.pdf"
    assert bad[0]["legacy"] is True and bad[0]["no_target"] is False


def test_audit_converted_cli_exit_code_only_counts_real_targets(tmp_path, monkeypatch, capsys):
    """99 区里"从未写过 converted_to"的 superseded 行不该让审计退 1（它们本来就没后继）；
    只有"写了目标却解析不到"才是真问题。"""
    import json as _json
    db_path = tmp_path / "kb.sqlite"
    conn = db.connect(db_path)
    for h, name, status_, note in (("aa" * 8, "a.pdf", "superseded", "duplicate"),
                                   ("bb" * 8, "b.doc", "superseded", "converted_to:没有.docx")):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path,"
            " dir_path, status, batch_id, first_seen_at, last_seen_at, note) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, "0" * 64, name, ".pdf", 1, str(tmp_path / name), "99-重复文件待删", status_,
             "t", "now", "now", note))
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    monkeypatch.setattr(config, "CORPUS_ROOT", tmp_path)
    monkeypatch.setattr(config, "QUESTION_MD_DIR", tmp_path)
    from mianjing_kb import verify_ledger as vl
    assert vl.main(["--audit-converted"]) == 1          # b.doc 写了目标却解析不到
    out = _json.loads(capsys.readouterr().out)
    assert out["superseded_rows"] == 2
    assert out["unresolved_with_target"] == 1 and out["no_target_rows"] == 1

    conn = db.connect(db_path, create=False)            # 把 b.doc 也改成无目标，应放行
    conn.execute("UPDATE source_documents SET note='duplicate' WHERE doc_hash=?", ("bb" * 8,))
    conn.commit()
    conn.close()
    assert vl.main(["--audit-converted"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["unresolved_with_target"] == 0 and out["no_target_rows"] == 2

def test_g7_flags_missing_doc_link(tmp_path):
    """README 引用的 docs 路径被归档/改名后必须报警（2026-11-19 R3 归档踩过）。"""
    (tmp_path / "README.md").write_text("# t\n\n- `docs/99-missing.md`\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    g = vl.g7_docs(tmp_path, actual_tests=0)
    assert g["state"] == "WARN"
    assert [x["kind"] for x in g["fails"]] == ["readme_doc_missing"]

def test_g1_invalid_skip_reason_does_not_exempt(mini_env):
    """空/拼错的 skip reason 不能放行；必须报 skip_reason_invalid。"""
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")
    out.commit()
    g = vl.g1_coverage(mini_env["kb"], out, {"02-Java进阶/乙.pdf": "typo_reason"})
    assert g["state"] == "FAIL"
    assert any(f.get("skip_reason") == "typo_reason" for f in g["fails"])


def test_g5_empty_src_path_counts_as_missing(mini_env):
    """Path('') == Path('.') 会把空 src_path 判成存在；必须先判空。"""
    kb = mini_env["kb"]
    kb.execute("UPDATE source_documents SET src_path='' WHERE file_name='甲.pdf'")
    kb.commit()
    g = vl.g5_disk(kb, mini_env["corpus"], mini_env["build"])
    assert any(f["kind"] == "classified_src_missing" for f in g["fails"])


def test_g6_schema_fail_is_hard_level(mini_env):
    """G6 的 schema 不合格必须能进 hard_fail（不再用 '软→硬' 裸字符串）。"""
    kb = mini_env["kb"]
    kb.execute("INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, provider, "
               "model, redacted, latency_ms, tokens, labeled_at) VALUES "
               "(2,'','玄学',9,'oops','p','m',0,1,1,'now')")
    kb.commit()
    g = vl.g6_label(kb)
    assert g["state"] == "FAIL" and g["level"] == vl.LEVEL_HARD

def test_g1_duplicate_of_target_must_exist(mini_env):
    """duplicate_of 带 target 且目标存在 → 可豁免；目标不存在 → FAIL。"""
    out = mini_env["out"]
    out.execute("DELETE FROM questions WHERE qid=2")
    out.commit()
    rel = "02-Java进阶/乙.pdf"
    good = {rel: {"rel": rel, "reason": "duplicate_of", "target": "02-Java进阶/甲.pdf"}}
    g = vl.g1_coverage(mini_env["kb"], out, {rel: "duplicate_of"}, good)
    assert g["state"] == "PASS"
    bad = {rel: {"rel": rel, "reason": "duplicate_of", "target": "02-Java进阶/不存在.pdf"}}
    g2 = vl.g1_coverage(mini_env["kb"], out, {rel: "duplicate_of"}, bad)
    assert g2["state"] == "FAIL"
    assert any(f.get("target") == "02-Java进阶/不存在.pdf" for f in g2["fails"])
