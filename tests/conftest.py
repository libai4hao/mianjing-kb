# -*- coding: utf-8 -*-
"""测试夹具：仓库根由 `pyproject.toml` 的 `pythonpath = ["."]` 提供，无需再插 sys.path。

唯一要做的是把 "kb" 别名绑到 `mianjing_kb.kb`（见该包 __init__.py），
保证历史上 `from kb import ...` 的写法与 `from mianjing_kb.kb import ...` 是同一实例。
"""
import importlib

importlib.import_module("mianjing_kb.kb")
