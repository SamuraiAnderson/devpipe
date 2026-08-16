"""AppShell 布局的真实渲染契约（doc/core-lib-requirements.md § CORE-11 + §5/19）。

这些断言此前是 ``styles.css`` 的字符串检查（"有没有 `calc(100vh -`"、"有没有裸
`#timeline-root` 规则"）——那只能证明"有人写过某条规则"，证明不了布局真的成立。
这里换成让浏览器算：视口高度、bounding box、computed style。

规格映射：
    §CORE-11/web/e2e/viewport-lock       → test_page_has_no_document_scrollbar
    §CORE-11/web/e2e/hidden-panel        → test_hidden_run_detail_takes_no_space
    §CORE-11/web/e2e/scroll-containers   → test_scrolling_happens_inside_containers
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import redpymake as rpm
    from playwright.sync_api import Page

# 起 uvicorn + 冷启 chromium + 跑脚本，默认 30s 硬超时不够用
pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]


def test_page_has_no_document_scrollbar(app_page: tuple["Page", "rpm.Workspace"]) -> None:
    """§CORE-11/web/e2e/viewport-lock：整页吃满视口，不出现文档级滚动条。"""
    page, _ws = app_page
    metrics = page.evaluate(
        """() => ({
            docScroll: document.documentElement.scrollHeight,
            viewport: window.innerHeight,
            bodyOverflow: getComputedStyle(document.body).overflowY,
        })"""
    )
    # 1px 容差：亚像素布局下 scrollHeight 会向上取整
    assert metrics["docScroll"] <= metrics["viewport"] + 1, (
        f"文档高 {metrics['docScroll']}px 超过视口 {metrics['viewport']}px，"
        "说明 body.app-shell 的视口锁定失效了"
    )
    assert metrics["bodyOverflow"] == "hidden"


def test_hidden_run_detail_takes_no_space(app_page: tuple["Page", "rpm.Workspace"]) -> None:
    """§CORE-11/web/e2e/hidden-panel：隐藏态 RunDetailView 不占位，LiveView 独占全高。

    历史坑：ID 选择器 ``#timeline-root { flex: 1 }`` (1,0,0) 会压过
    ``.panel-view[hidden]`` (0,2,0)，隐藏的回放面板继续按 flex 占位，把 LiveView
    挤成半高。这里直接量两者的 bounding box，而不是去猜样式表怎么写。
    """
    page, _ws = app_page
    detail = page.locator("#timeline-root")
    assert detail.get_attribute("hidden") is not None, "初始态应停在 Live 视图"
    # 隐藏元素在 Playwright 里没有 box；有 box 就说明它在占位
    assert detail.bounding_box() is None

    live_box = page.locator("#live-root").bounding_box()
    panel_box = page.locator("main.main-panel").bounding_box()
    assert live_box is not None and panel_box is not None
    assert live_box["height"] >= panel_box["height"] - 2, (
        f"LiveView 高 {live_box['height']}px 未吃满 MainPanel 的 "
        f"{panel_box['height']}px，隐藏的 RunDetailView 大概在抢空间"
    )


def test_scrolling_happens_inside_containers(
    page_with_log_rows: tuple["Page", "rpm.Workspace"],
) -> None:
    """§CORE-11/web/e2e/scroll-containers：滚动发生在内部容器，不在文档上。

    只验 Live 态的两个容器（Sidebar / LiveBody）；第三个 TimelineBody 只在回放态
    存在，由 ``test_e2e_run_flow.py`` 的回放用例覆盖。
    """
    page, _ws = page_with_log_rows
    for selector in (".sidebar", "#live-root ol.live-body"):
        overflow = page.eval_on_selector(selector, "el => getComputedStyle(el).overflowY")
        assert overflow in ("auto", "scroll"), f"{selector} 不是滚动容器（overflowY={overflow}）"

    # LiveBody 真的能滚，且滚它不会带动文档
    scrolled = page.eval_on_selector(
        "#live-root ol.live-body",
        """el => {
            el.scrollTop = el.scrollHeight;
            return { top: el.scrollTop, docTop: document.documentElement.scrollTop };
        }""",
    )
    assert scrolled["docTop"] == 0, "内部容器滚动不应带动文档滚动"
