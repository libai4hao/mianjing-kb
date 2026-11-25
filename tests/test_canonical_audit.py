# -*- coding: utf-8 -*-
"""canonical 星形审计测试：代表题直连、合法低相似对、改写证据、候选带。"""
from mianjing_kb import canonical as cn
from mianjing_kb import canonical_audit as ca

A = "mysql索引为什么用b+树不用b树"     # 与 B 0.812、与 C 0.611
B = "mysql索引为什么用b+树"            # 与 C 0.733
C = "为什么mysql索引用b+树"
JVM = "什么是jvm内存模型"
BAND_A = "什么是springmvc"             # 与 BAND_B 0.692：落在 [0.65, 0.7) 候选带
BAND_B = "springmvc是什么"


def _q(norm: str, doc: str, text: str | None = None) -> dict:
    return {"q_norm": norm, "simhash_u": 0, "doc_hash": doc, "qtype": "八股",
            "collection": f"c-{doc}", "q_text": text or norm, "q_key": f"{doc}:1"}


def _dataset() -> list[dict]:
    """7 题：JVM 同文×2、MySQL 三题链式、SpringMVC 候选带×2。"""
    return [_q(JVM, "h1"), _q(JVM, "h2"),
            _q(A, "h3"), _q(B, "h4"), _q(C, "h5"),
            _q(BAND_A, "h6"), _q(BAND_B, "h7")]


def test_audit_counts_positive_merges_and_legal_star():
    data = ca.audit_questions(_dataset())
    assert data["questions"] == 7 and data["clusters"] == 4
    assert data["merged_away"] == 3
    # >=0.7 的 3 对（JVM 同文、A-B、B-C）全部同簇，0 跨簇
    assert data["pairs_jaccard_ge_jac"] == 3
    assert data["pairs_ge_jac_same_cluster"] == 3
    assert data["pairs_ge_jac_cross_cluster"] == 0
    assert data["violators"] == []
    # A-C 0.611 虽低，但 A、C 都直接相似于代表题 B，属于合法星形
    assert data["pairs_within_cluster"] == 4
    assert data["pairs_within_below_jac"] == 1
    assert data["chain_samples"][0]["jac"] == 0.611
    assert data["pairs_member_below_anchor"] == 0
    assert data["star_violation_samples"] == []
    # 改写证据：MySQL 三题两两文本都不同（3 对），不因字面差异被拆
    assert data["pairs_within_paraphrase"] == 3
    assert data["rewrite_samples"]
    # 候选带：0.65–0.7 只计数、不合并
    assert data["pairs_in_band"] == 1


def test_audit_allows_high_similarity_pairs_across_competing_stars():
    tail = "为什么mysql索引使用b+树"
    qs = _dataset()[:5] + [_q(tail, "h8")]
    assert cn.similarity(C, tail) >= cn.ATTACH_JAC
    assert cn.similarity(A, tail) < cn.ATTACH_JAC
    data = ca.audit_questions(qs)
    assert data["pairs_ge_jac_cross_cluster"] == 1
    assert data["violators"]
    assert data["pairs_member_below_anchor"] == 0
    assert data["star_violation_samples"] == []
    assert "PASS" in ca.render_md(data)


def test_audit_samples_are_deterministic_thresholds():
    """阈值口径自证：用例里的“该合/不该合”由 similarity 直接给出，不靠手写常数。"""
    assert cn.similarity(A, B) >= cn.ATTACH_JAC
    assert cn.similarity(B, C) >= cn.ATTACH_JAC
    assert cn.similarity(A, C) < cn.ATTACH_JAC          # 链式传递的成因
    assert cn.CANDIDATE_JAC <= cn.similarity(BAND_A, BAND_B) < cn.ATTACH_JAC


def test_render_md_does_not_fail_on_competing_stars():
    data = {"db": "x.db", "questions": 2, "clusters": 2, "merged_away": 0,
            "multi_doc_clusters": 0, "cluster_size_dist": {"1": 2},
            "pairs_within_cluster": 0, "pairs_within_below_jac": 0,
            "pairs_within_paraphrase": 0, "pairs_jaccard_ge_jac": 1,
            "pairs_ge_jac_same_cluster": 0, "pairs_ge_jac_cross_cluster": 1,
            "pairs_member_below_anchor": 0, "star_violation_samples": [],
            "pairs_in_band": 0, "rewrite_samples": [], "chain_samples": [],
            "violators": [{"jac": 0.8, "a": "x", "b": "y"}], "band": 0.65}
    md = ca.render_md(data)
    assert "0/1" in md and "PASS" in md and "星形竞争" in md


def test_render_md_flags_star_anchor_violation():
    data = {"db": "x.db", "questions": 2, "clusters": 1, "merged_away": 1,
            "multi_doc_clusters": 0, "cluster_size_dist": {"2": 1},
            "pairs_within_cluster": 1, "pairs_within_below_jac": 1,
            "pairs_within_paraphrase": 1, "pairs_jaccard_ge_jac": 0,
            "pairs_ge_jac_same_cluster": 0, "pairs_ge_jac_cross_cluster": 0,
            "pairs_member_below_anchor": 1,
            "star_violation_samples": [{"jac": 0.5, "representative": "x", "member": "y"}],
            "pairs_in_band": 0, "rewrite_samples": [], "chain_samples": [],
            "violators": [], "band": 0.65}
    md = ca.render_md(data)
    assert "FAIL" in md and "星形违规样例" in md
