"""
M4 · 极简 .env 加载器（纯标准库，无 python-dotenv 依赖）
=====================================================
为什么需要它
------------
from_env() 用 os.getenv 读配置，但 Python 默认不读 .env 文件。
本模块在 import models 时自动把项目根目录的 .env 注入 os.environ，
这样 `cp .env.example .env` 填好值后即可直接生效，无需手动 export。

安全
----
- 用 os.environ.setdefault：已存在的 OS 环境变量优先（不覆盖 shell 显式设置）。
- 文件缺失时静默 no-op；不打印、不写日志任何密钥。
"""
from __future__ import annotations

import os


def _project_root() -> str:
    # models/env.py -> 项目根在两级上层
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> None:
    """把 .env 文件注入 os.environ。现有 OS 环境变量优先。文件缺失则静默跳过。

    :param path: 显式指定 .env 路径（测试用）；否则依次查找 cwd/.env、项目根/.env。
    """
    candidates: list[str] = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(os.getcwd(), ".env"))
    candidates.append(os.path.join(_project_root(), ".env"))

    target = next((c for c in candidates if os.path.isfile(c)), None)
    if not target:
        return

    with open(target, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # 去除引号包裹（' 或 "）
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
