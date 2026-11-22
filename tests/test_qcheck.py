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
