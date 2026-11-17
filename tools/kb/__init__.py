# -*- coding: utf-8 -*-
"""kb 公共库：config（路径与阈值）、db（SQLite）、hashing（指纹）、textmetrics（文本质量）。

双路径导入统一：仓库根与 tools/ 都可能在 sys.path，`kb` 与 `tools.kb` 会成为两个
独立模块实例（P1-5 踩坑：测试 monkeypatch tools.kb.config 不影响 classify 里的
kb.config）。这里把本实例注册为顶层 "kb"（若顶层名尚空），配合 conftest 先导入
本包，保证两个名字拿到同一实例。
"""
import sys as _sys

_sys.modules.setdefault("kb", _sys.modules[__name__])
