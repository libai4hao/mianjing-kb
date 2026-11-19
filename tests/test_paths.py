# -*- coding: utf-8 -*-
"""路径边界：清单/语料相对路径不得越出根目录。"""
import pytest

from mianjing_kb.kb import paths


def test_safe_join_accepts_nested_rel(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "b.txt").write_text("x", encoding="utf-8")
    assert paths.safe_join(root, "a/b.txt") == (root / "a/b.txt").resolve()


@pytest.mark.parametrize("rel", ["../outside.txt", "/abs/outside.txt", "C:/abs.txt", "", "a/../b"])
def test_safe_join_rejects_escape(tmp_path, rel):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(paths.UnsafePath):
        paths.safe_join(root, rel)


def test_safe_join_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台/权限不支持 symlink")
    with pytest.raises(paths.UnsafePath):
        paths.safe_join(root, "link.txt", must_exist=True)
