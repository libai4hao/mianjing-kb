# -*- coding: utf-8 -*-
"""遍历边界：junction / 软链目录不进语料遍历（读语料外正文、写语料外路径）。"""
import os
import subprocess

import pytest

from mianjing_kb.kb import db as kbdb
from mianjing_kb.kb import mdmap, paths


def _make_dir_link(link, target) -> bool:
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        return r.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def _drop_dir_link(link) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
    else:
        os.unlink(link)


def test_walk_files_skips_link_directory(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside", encoding="utf-8")
    link = corpus / "link"
    if not _make_dir_link(link, outside):
        pytest.skip("当前环境不能创建 junction/软链")
    try:
        found = [p.name for p in paths.walk_files(corpus, (".md",))]
        assert found == ["a.md"]
        assert list(paths.walk_files(corpus, (".md",))) == [corpus / "a.md"]
    finally:
        _drop_dir_link(link)


def test_mdmap_ignores_md_reached_through_link(tmp_path, monkeypatch):
    from mianjing_kb.kb import config

    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(config, "BUILD_DIR", build)
    conn = kbdb.connect(build / "kb.sqlite")

    md_root = tmp_path / "md"
    (md_root / "综合").mkdir(parents=True)
    (md_root / "综合" / "真件.md").write_text("# 题", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "越界.md").write_text("# 语料外", encoding="utf-8")
    link = md_root / "link"
    if not _make_dir_link(link, outside):
        pytest.skip("当前环境不能创建 junction/软链")
    try:
        m = mdmap.build(conn, md_root)
        assert m.report["outside_root"] >= 1
        rels = {p["rel"] for p in m.products}
        assert "link/越界.md" not in rels
    finally:
        _drop_dir_link(link)
        conn.close()
