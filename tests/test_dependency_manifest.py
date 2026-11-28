# -*- coding: utf-8 -*-
"""依赖清单一致性：requirements.txt 必须与 pyproject 的运行时依赖 + [dev] 严格等价。

回归背景：CI 装完 requirements.txt 后跑 pytest，`tests/test_xlsx_budget.py` 因缺
openpyxl 在收集阶段 ImportError——两个清单各自维护、没有交叉校验。现在
requirements.txt 是文档化的开发环境入口，pyproject 是 pip 装包的事实来源，
任一侧漏写/写错都会让本用例失败，而不是等 CI 跑到一半才暴露。
"""
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[1]


def _key(spec):
    """归一化 (名字, 版本约束, 环境标记)：去大小写、去空白、引号统一成单引号。"""
    req = Requirement(spec)
    marker = str(req.marker or "").replace('"', "'").replace(" ", "")
    return (req.name.lower(), str(req.specifier), marker)


def _from_requirements():
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    specs = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        specs.append(_key(line))
    return sorted(specs)


def _from_pyproject():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specs = list(project["dependencies"])
    specs += list(project.get("optional-dependencies", {}).get("dev", []))
    return sorted(_key(s) for s in specs)


def test_requirements_matches_pyproject():
    req, proj = _from_requirements(), _from_pyproject()
    missing = [x for x in proj if x not in req]
    extra = [x for x in req if x not in proj]
    assert not missing, f"requirements.txt 缺少 pyproject 声明的依赖: {missing}"
    assert not extra, f"requirements.txt 多出 pyproject 未声明的依赖: {extra}"
