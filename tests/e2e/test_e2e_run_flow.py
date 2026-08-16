"""从点 ▶ 到看见输出的完整链路（doc/core-lib-requirements.md § CORE-11 + §5/18）。

这是 WebSocket 推送的**首个**自动化覆盖：LiveBody 里的记录只能来自 ``onWsEvent``
的 ``run.record`` / ``run.meta``（列表的兜底轮询只刷 sessions / runs / logs，不刷
记录），所以"输出出现在页面上"就等价于"WS 端到端通了"。

规格映射：
    §CORE-11/web/e2e/run-action      → test_clicking_run_action_streams_output_to_live
    §CORE-11/web/e2e/run-separator   → test_clicking_run_action_streams_output_to_live
    §CORE-11/web/e2e/two-views       → test_run_detail_and_back_to_live_round_trip
    §CORE-11/web/e2e/scroll-containers（TimelineBody 部分）
                                     → test_run_detail_and_back_to_live_round_trip
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .conftest import RUN_TIMEOUT_MS

if TYPE_CHECKING:
    import redpymake as rpm
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]


def test_clicking_run_action_streams_output_to_live(
    app_page: tuple["Page", "rpm.Workspace"],
) -> None:
    """§CORE-11/web/e2e/run-action：点 ScriptItem 的 ▶ 后，输出经 WS 落到 LiveBody。"""
    page, ws = app_page

    page.get_by_title("Run hello").click()

    # 脚本 stdout 经 WS 推到 LiveBody；这条一过就说明 enqueue + WS + 渲染都通了
    page.locator(
        "#live-root ol.live-body li.tl-row", has_text="E2E-HELLO"
    ).first.wait_for(timeout=RUN_TIMEOUT_MS)

    # workspace.run.begin/end 元行驱动的流内 run 边界
    page.locator("#live-root li.run-separator").first.wait_for(timeout=RUN_TIMEOUT_MS)

    # Sidebar 的 StatusBadge 走到终态
    page.locator(
        "#runs-list li.sb-item .badge.status-succeeded"
    ).first.wait_for(timeout=RUN_TIMEOUT_MS)

    # 服务端侧同样是成功终态——UI 显示的不是自己编的
    runs = list(ws.runs)
    assert len(runs) == 1
    assert runs[0].status == "succeeded"


def test_run_detail_and_back_to_live_round_trip(
    page_with_log_rows: tuple["Page", "rpm.Workspace"],
) -> None:
    """§CORE-11/web/e2e/two-views：点 RunItem 进回放态，Back to Live 能原路返回。

    顺带验第三个内部滚动容器 TimelineBody——它只在回放态存在，所以不在
    ``test_e2e_app_shell.py`` 的覆盖范围里。
    """
    page, _ws = page_with_log_rows
    page.locator(
        "#runs-list li.sb-item .badge.status-succeeded"
    ).first.wait_for(timeout=RUN_TIMEOUT_MS)

    # 只有 RunItem 的 .sb-label 带 onclick；点 li 本身是无副作用的（防误触）
    page.locator("#runs-list li.sb-item .sb-label").first.click()

    page.locator("#timeline-root .pane-set").wait_for()
    assert page.evaluate("() => window.__rpmStore.getState().view") == "run"
    assert page.locator("#main-toolbar").is_visible(), "回放态才出现 MainToolbar"
    assert page.locator("#live-root").get_attribute("hidden") is not None
    # 回放专属控件：Live·Play·Reset·Step·Speed·Scrubber
    assert page.locator("#timeline-root .timeline-toolbar").is_visible()

    overflow = page.eval_on_selector(
        "#timeline-root ol.timeline-body", "el => getComputedStyle(el).overflowY"
    )
    assert overflow in ("auto", "scroll"), f"TimelineBody 不是滚动容器（overflowY={overflow}）"

    page.locator("#back-to-live").click()
    page.locator("#live-root ol.live-body").wait_for()
    assert page.evaluate("() => window.__rpmStore.getState().view") == "live"
    assert page.locator("#live-root").get_attribute("hidden") is None
    assert page.locator("#timeline-root").get_attribute("hidden") is not None
