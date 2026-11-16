# -*- coding: utf-8 -*-
"""测试夹具：把仓库根加进 sys.path，便于 import tools.* 与 kb.*。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
