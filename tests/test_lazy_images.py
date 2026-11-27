# -*- coding: utf-8 -*-
"""lazy_images 回归（审计候选 #31/#32）：备份撞名、备份校验、链接剪枝、原子改写。"""
import json

import pytest

from mianjing_kb import lazy_images as li


@pytest.fixture()
def env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    md = corpus / "20-题库MD"
    build = tmp_path / "build"
    md.mkdir(parents=True)
    build.mkdir()
    monkeypatch.setattr(li, "MD_ROOT", md)
    monkeypatch.setattr(li, "BACKUP", build / "lazyimg_backup")
    return {"md": md, "build": build}


def _write(md, rel, text):
    p = md / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_colliding_flattened_paths_both_get_backups(env, capsys):
    """#31：a/b.md 与 a_b.md 平铺后同名——双哈希键必须让两份原文都可回滚。"""
    a = _write(env["md"], "a/b.md", "甲文\n\n![图](./images/x.png)\n")
    b = _write(env["md"], "a_b.md", "乙文\n\n![图](./images/y.png)\n")
    old_a, old_b = a.read_bytes(), b.read_bytes()
    assert li.main([]) == 0
    capsys.readouterr()
    baks = list((env["build"] / "lazyimg_backup").glob("*.md"))
    assert len(baks) == 2
    contents = {p.read_bytes() for p in baks}
    assert contents == {old_a, old_b}          # 两份原文都在，无一丢失
    assert "<img " in a.read_text(encoding="utf-8")
    assert "<img " in b.read_text(encoding="utf-8")


def test_backup_name_changes_with_content(env):
    n1 = li.backup_name("a/b.md", b"one")
    n2 = li.backup_name("a/b.md", b"two")
    n3 = li.backup_name("a_b.md", b"one")
    assert n1 != n2 and n1 != n3


def test_tampered_backup_blocks_rewrite(env, capsys):
    """#31：备份内容与原文不符时拒绝改写该文件并非 0 退出。"""
    p = _write(env["md"], "a.md", "正文\n\n![图](./images/x.png)\n")
    old = p.read_bytes()
    bak_dir = env["build"] / "lazyimg_backup"
    bak_dir.mkdir(parents=True)
    (bak_dir / li.backup_name("a.md", old)).write_bytes(b"tampered")
    assert li.main([]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["backup_failed"] == ["a.md"]
    assert p.read_bytes() == old               # 原文未被改写


def test_junction_md_is_not_rewritten(env, capsys):
    """#32：md 根内的 junction/软链不跟随——外部哨兵不得被读入备份或改写。"""
    outside = env["build"] / "outside"
    outside.mkdir()
    sentinel = outside / "victim.md"
    sentinel.write_text("外部正文\n\n![图](./images/x.png)\n", encoding="utf-8")
    link = env["md"] / "link.md"
    try:
        link.symlink_to(sentinel)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted on this host")
    before = sentinel.read_bytes()
    assert li.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files_changed"] == 0
    assert sentinel.read_bytes() == before
    assert not (env["build"] / "lazyimg_backup").exists()


def test_rewrite_is_atomic_and_idempotent(env, capsys):
    p = _write(env["md"], "a.md", "正文\n\n![图](./images/x.png)\n")
    assert li.main([]) == 0
    capsys.readouterr()
    once = p.read_bytes()
    assert li.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files_changed"] == 0           # 幂等：已是 <img 的不再动
    assert p.read_bytes() == once
    assert not list(env["md"].glob(".*.tmp"))  # 原子写无残留临时件
