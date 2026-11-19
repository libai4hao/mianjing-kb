# -*- coding: utf-8 -*-
"""D-37 集成层：真实 BGE 模型契约。只在受控环境/夜间跑（RUN_BGE_INTEGRATION=1）。

普通 pytest 默认跳过，避免联网下载模型；模型与版本固定为 BAAI/bge-small-zh-v1.5。
"""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastembed")
from fastembed import TextEmbedding  # noqa: E402

MODEL = "BAAI/bge-small-zh-v1.5"
DIM = 512


def _enabled() -> bool:
    return os.environ.get("RUN_BGE_INTEGRATION") == "1"


@pytest.fixture(scope="module")
def model():
    if not _enabled():
        pytest.skip("RUN_BGE_INTEGRATION!=1，跳过真实模型集成测试")
    return TextEmbedding(MODEL)


def _files_fingerprint(model_name: str) -> str:
    """模型文件指纹：固定版本路径下所有文件的相对路径 + 大小。"""
    cache = Path(os.environ.get("FASTEMBED_CACHE_PATH", "")) if os.environ.get("FASTEMBED_CACHE_PATH") else None
    if not cache or not cache.exists():
        return "cache-path-unset"
    entries = []
    for p in sorted(cache.rglob("*")):
        if p.is_file():
            entries.append(f"{p.relative_to(cache)}:{p.stat().st_size}")
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def test_real_model_dimension_and_finite(model):
    vecs = list(model.embed(["MySQL 索引为什么用 B+ 树"]))
    assert len(vecs[0]) == DIM
    assert all(abs(float(v)) < 1e6 for v in vecs[0])
    print(json.dumps({"model": MODEL, "files_fingerprint": _files_fingerprint(MODEL)},
                     ensure_ascii=False))


def test_real_model_same_input_is_stable(model):
    a = list(model.embed(["同一句查询"]))[0]
    b = list(model.embed(["同一句查询"]))[0]
    assert max(abs(float(x) - float(y)) for x, y in zip(a, b)) < 1e-5


def test_real_model_throughput_shape(model):
    texts = [f"第 {i} 条检索测试文本，主题 Java 并发" for i in range(20)]
    start = time.perf_counter()
    vecs = list(model.embed(texts))
    elapsed = time.perf_counter() - start
    assert len(vecs) == 20 and all(len(v) == DIM for v in vecs)
    print(json.dumps({"model": MODEL, "dim": DIM, "texts": len(texts),
                      "seconds": round(elapsed, 3),
                      "per_second": round(len(texts) / elapsed, 2)}, ensure_ascii=False))
    assert elapsed < 120
