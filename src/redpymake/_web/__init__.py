"""Web UI 子包 (CORE-11)。

``redpymake[web]`` extra 才装 FastAPI / Jinja2 / uvicorn；缺失时导入这里的
``server`` 会以 ``ImportError`` 失败，由 CLI 层给用户友好提示。

``report`` 模块不依赖 FastAPI/Jinja2，即使核心安装也能生成静态 HTML。
"""

from __future__ import annotations
