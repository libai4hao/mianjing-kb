# -*- coding: utf-8 -*-
"""qcheck：固定样本集的抽样口径与统计口径。"""
from mianjing_kb.qcheck import make_set, stats


def _rows(ok=8, gap=4, miss=4, toc=2):
    rows = []
    qid = 1
    for st, n in (("ok", ok), ("boundary_gap", gap),
                  ("no_answer_source", miss), ("toc_noise", toc)):
        for _ in range(n):
            rows.append({"qid": qid, "doc_hash": f"d{qid:04d}", "qtype": "八股",
                         "doc": "x.md", "answer_state": st, "q": "q", "a": "a" if st == "ok" else ""})
            qid += 1
    return rows


class TestMakeSet:
    def test_counts_match_quotas(self):
        payload = make_set(_rows(), n=18,
                           quotas={"ok": 8, "boundary_gap": 4, "no_answer_source": 4, "toc_noise": 2})
        assert payload["n"] == 18
        assert sum(1 for x in payload["items"] if x["answer_state"] == "ok") == 8
        assert payload["pool_sizes"]["toc_noise"] == 2

    def test_same_seed_same_sample(self):
        a = make_set(_rows(), n=18, seed=7)
        b = make_set(_rows(), n=18, seed=7)
        assert [x["qid"] for x in a["items"]] == [x["qid"] for x in b["items"]]

    def test_quota_scaled_and_capped_by_pool(self):
        payload = make_set(_rows(toc=1), n=100,
                           quotas={"ok": 10, "boundary_gap": 10, "no_answer_source": 10, "toc_noise": 10})
        assert sum(1 for x in payload["items"] if x["answer_state"] == "toc_noise") == 1


class TestStats:
    def _items(self):
        return [
            {"qid": 1, "answer_state": "ok", "label": "pass"},
            {"qid": 2, "answer_state": "ok", "label": "fail"},
            {"qid": 3, "answer_state": "ok", "label": None},
            {"qid": 4, "answer_state": "no_answer_source", "label": "fail"},
            {"qid": 5, "answer_state": "boundary_gap", "label": "partial"},
        ]

    def test_strict_and_usable(self):
        r = stats(self._items(), {})
        assert r["judged"] == 4 and r["unlabeled"] == 1
        assert r["strict_accuracy"] == 0.25
        assert r["usable_rate"] == 0.5

    def test_answered_scope_excludes_missing_source(self):
        r = stats(self._items(), {})
        # ok + boundary_gap：pass 1 / fail 1 / partial 1 → 严格 1/3，可用 2/3
        assert r["answered_scope"]["judged"] == 3
        assert r["answered_scope"]["strict_accuracy"] == 0.3333
        assert r["answered_scope"]["usable_rate"] == 0.6667

    def test_labels_file_overrides_embedded(self):
        r = stats(self._items(), {"1": {"label": "fail"}})
        assert r["counts"]["pass"] == 0 and r["counts"]["fail"] == 3

    def test_no_labels_means_no_accuracy(self):
        r = stats([{"qid": 9, "answer_state": "ok", "label": None}], {})
        assert r["strict_accuracy"] is None and r["usable_rate"] is None


class TestIdentityBinding:
    """#69：carry/render 按稳定身份绑定，qid 漂移不得把旧标签挂到新题。"""

    def _db(self, tmp_path, q_text="什么是 JVM？", q_key="d1:h1"):
        from mianjing_kb.kb import db as kbdb
        conn = kbdb.connect(tmp_path / "kb.sqlite")
        conn.execute(
            "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, span_start, span_end,"
            " qtype, conf, q_norm, q_norm_hash, simhash, q_key, answer_state, extracted_at)"
            " VALUES (1,'d1',1,?,?,0,1,'八股','high','norm','h1',0,?,'ok','now')",
            (q_text, "答案", q_key))
        conn.commit()
        return conn

    def test_render_marks_stale_on_qkey_mismatch(self, tmp_path):
        from mianjing_kb import qcheck
        conn = self._db(tmp_path, q_text="什么是 Redis？", q_key="d1:h2")
        items = [{"qid": 1, "q_key": "d1:h1", "q_norm": "什么是jvm",
                  "answer_state": "ok", "label": "pass", "reason": "旧判"}]
        md, detail = qcheck.render(conn, items)
        conn.close()
        assert detail[0]["stale"] == "q_key_mismatch"
        assert detail[0]["label"] is None          # 旧标签不作数
        assert "已失效" in md

    def test_render_missing_qid_is_explicit_stale(self, tmp_path):
        from mianjing_kb import qcheck
        conn = self._db(tmp_path)
        items = [{"qid": 99, "q_key": "d9:h9", "answer_state": "ok", "label": "pass"}]
        _md, detail = qcheck.render(conn, items)
        conn.close()
        assert detail[0]["stale"] == "qid_missing"  # 不再静默跳过

    def test_carry_matches_by_qkey_not_qid(self, tmp_path, monkeypatch, capsys):
        import json

        from mianjing_kb import qcheck
        # 当前库：qid=1 已是另一题（h2）；旧样本判的是 h1（现 qid=2）
        conn = self._db(tmp_path, q_text="什么是 Redis？", q_key="d1:h2")
        conn.execute(
            "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, span_start, span_end,"
            " qtype, conf, q_norm, q_norm_hash, simhash, q_key, answer_state, extracted_at)"
            " VALUES (2,'d1',2,'什么是 JVM？','答案',0,1,'八股','high','norm2','h1b',0,"
            "'d1:h1','ok','now')")
        conn.commit()
        conn.close()
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"items": [
            {"qid": 1, "q_key": "d1:h1", "label": "pass", "reason": "旧判"}]}), encoding="utf-8")
        out = tmp_path / "new.json"
        rc = qcheck.main(["make", "--db", str(tmp_path / "kb.sqlite"), "--n", "2",
                          "--carry", str(old), "--out", str(out)])
        assert rc == 0
        capsys.readouterr()
        payload = json.loads(out.read_text(encoding="utf-8"))
        carried = [x for x in payload["items"] if x.get("label")]
        assert len(carried) == 1 and carried[0]["qid"] == 2   # 挂到 h1 的当前行
        assert carried[0]["q_key"] == "d1:h1"
        # qid=1（现为 Redis 题）不得带上 JVM 的旧标签
        assert all(x.get("label") is None for x in payload["items"] if x["qid"] == 1)
