"""Web UI 浏览器端到端测试（``pytest -m e2e``）。

默认不运行：需要 ``redpymake[e2e]`` 与 ``playwright install chromium``。
规格见 doc/core-lib-requirements.md § CORE-11「Web UI 端到端验证（Playwright）」。
定位一律走部件 ``aria-label``（``by_name``），不用 class / id 选择器。
"""
