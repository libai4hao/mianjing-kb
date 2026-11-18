# -*- coding: utf-8 -*-
"""kb 公共库：config（路径与阈值）、db（SQLite）、hashing（指纹）、textmetrics（文本质量）。

正规导入名是 `mianjing_kb.kb`（P1-5 踩坑：历史上有 `kb` 与 `tools.kb` 两个实例，
monkeypatch 一边不影响另一边）。改名后仍保留顶层别名 `kb`：老脚本和文档里
`from kb import config` 的写法继续可用，且与 `mianjing_kb.kb` 是**同一个模块对象**，
所以 monkeypatch/config 都只存在一份。
"""
import sys as _sys

_sys.modules.setdefault("kb", _sys.modules[__name__])
