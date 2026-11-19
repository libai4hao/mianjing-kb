# -*- coding: utf-8 -*-
"""D-37 单元层：用 mock embedding 固定向量契约，每次 PR 可跑，不联网下载模型。"""
import math

import pytest

from mianjing_kb import embed_questions as E


class FakeModel:
    def embed(self, texts):
        out = []
        for i, text in enumerate(texts):
            vec = [0.0] * E.DIM
            vec[i % E.DIM] = 1.0
            out.append(vec)
        return out


def test_embed_texts_returns_expected_dimension(monkeypatch):
    monkeypatch.setattr(E, "get_model", lambda: FakeModel())
    vecs = E.embed_texts(["a", "b"])
    assert len(vecs) == 2 and all(len(v) == E.DIM for v in vecs)


def test_embed_texts_empty_input_does_not_load_model(monkeypatch):
    monkeypatch.setattr(E, "get_model", lambda: (_ for _ in ()).throw(RuntimeError("no model")))
    assert E.embed_texts([]) == []


def test_embed_texts_rejects_wrong_dimension(monkeypatch):
    class BadModel:
        def embed(self, texts):
            return [[0.1] * 3 for _ in texts]

    monkeypatch.setattr(E, "get_model", lambda: BadModel())
    with pytest.raises(ValueError, match="向量维度"):
        E.embed_texts(["a"])


def test_embed_texts_same_input_is_stable(monkeypatch):
    monkeypatch.setattr(E, "get_model", lambda: FakeModel())
    first = E.embed_texts(["同一条查询"])
    second = E.embed_texts(["同一条查询"])
    assert first == second


def test_embed_texts_no_nan_or_inf(monkeypatch):
    monkeypatch.setattr(E, "get_model", lambda: FakeModel())
    vecs = E.embed_texts(["x"])
    assert all(math.isfinite(v) for v in vecs[0])


def test_model_name_and_dim_are_pinned():
    assert E.MODEL == "BAAI/bge-small-zh-v1.5"
    assert E.DIM == 512
