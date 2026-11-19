# -*- coding: utf-8 -*-
"""ocr_merge 备份命名：扁平化不能撞名。"""
from mianjing_kb import ocr_merge as m


def test_backup_name_does_not_collapse_paths():
    assert m.backup_name("a/b_c.md", b"same") != m.backup_name("a_b/c.md", b"same")


def test_backup_name_tracks_content():
    assert m.backup_name("a/b_c.md", b"v1") != m.backup_name("a/b_c.md", b"v2")
