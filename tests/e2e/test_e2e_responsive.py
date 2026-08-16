"""LogRow 列宽的响应式行为（doc/core-lib-requirements.md § CORE-11 AppShell 布局约束）。

作用域是 **Run detail 的 LogRow**：四列 grid 与 ``@media (max-width: 960px)`` 只落在
``.tl-row`` 上，而 ``.live-body .tl-row`` 已经被覆盖成 [gutter | msg] 两列 flex，没有
SessionCell。之前的字符串断言（``".tl-sid { display: none; }" in css``）看不出这个
区别——它在 LiveBody 上是空转的。

规格映射：
    §CORE-11/web/e2e/session-cell-clamp   → test_session_cell_width_tracks_viewport
    §CORE-11/web/e2e/session-cell-narrow  → test_session_cell_hides_on_narrow_viewport
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from .conftest import RUN_TIMEOUT_MS

if TYPE_CHECKING:
    import redpymake as rpm
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]

# 带 SessionCell 的 LogRow：没有 session_id 的记录只渲染三个 span
_ROW = "#timeline-root li.tl-row:has(.tl-sid)"


def test_session_cell_hides_on_narrow_viewport(
    page_with_log_rows: tuple["Page", "rpm.Workspace"],
) -> None:
    """§CORE-11/web/e2e/session-cell-narrow：窄视口整列隐藏，消息列顶上第三列。"""
    page, _ws = page_with_log_rows
    _open_run_detail(page)

    page.set_viewport_size({"width": 1280, "height": 800})
    wide = _row_layout(page)
    assert wide["sidDisplay"] != "none", "宽视口下 SessionCell 应当可见"
    assert wide["msgColumn"] == "4"
    assert wide["columnCount"] == 4

    page.set_viewport_size({"width": 800, "height": 800})
    narrow = _row_layout(page)
    assert narrow["sidDisplay"] == "none", "窄视口下 SessionCell 应整列隐藏"
    assert narrow["msgColumn"] == "3", "消息必须顶上第三列，否则会错位到空出来的列"
    assert narrow["columnCount"] == 3


def test_session_cell_width_tracks_viewport(
    page_with_log_rows: tuple["Page", "rpm.Workspace"],
) -> None:
    """§CORE-11/web/e2e/session-cell-clamp：``clamp()`` 的 vw 项真的在收缩列宽。

    两个宽度都在 960px 断点以上，比的是 clamp 中段是否随视口线性变化——只断言
    "样式表里有 clamp(" 是看不出这件事的。
    """
    page, _ws = page_with_log_rows
    _open_run_detail(page)

    page.set_viewport_size({"width": 1280, "height": 800})
    wide = _row_layout(page)["sidWidth"]
    page.set_viewport_size({"width": 1000, "height": 800})
    narrow = _row_layout(page)["sidWidth"]

    assert narrow < wide, f"视口收窄后 SessionCell 列没变窄（{wide}px → {narrow}px）"
    # clamp(64px, 10vw, 200px) 的上下限
    assert 64 <= narrow <= 200


def _open_run_detail(page: "Page") -> None:
    """进回放态，并按 Live 让全部记录一次渲染完。

    已结束的 run 载入后默认从头自动播放，行是逐条出现的；点 Live 直接跳到全量，
    省掉"等播放走完"的不确定等待。
    """
    page.locator(
        "#runs-list li.sb-item .badge.status-succeeded"
    ).first.wait_for(timeout=RUN_TIMEOUT_MS)
    page.locator("#runs-list li.sb-item .sb-label").first.click()
    page.locator("#timeline-root .timeline-toolbar button:has-text('Live')").click()
    page.locator(_ROW).first.wait_for()


def _row_layout(page: "Page") -> dict[str, Any]:
    """量一行 LogRow 的真实列布局。"""
    return page.eval_on_selector(
        _ROW,
        """el => {
            const cols = getComputedStyle(el).gridTemplateColumns.split(/\\s+/).filter(Boolean);
            const sid = el.querySelector('.tl-sid');
            const msg = el.querySelector('.tl-msg');
            return {
                columnCount: cols.length,
                sidWidth: parseFloat(cols[2] || '0'),
                sidDisplay: sid ? getComputedStyle(sid).display : null,
                msgColumn: msg ? getComputedStyle(msg).gridColumnStart : null,
            };
        }""",
    )
