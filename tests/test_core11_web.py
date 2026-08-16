"""CORE-11 Web UI 契约（doc/core-lib-requirements.md § CORE-11）。

规格映射：
    §CORE-11/web/routes/index         → test_web_index_returns_html
    §CORE-11/web/dom-names            → test_web_index_exposes_named_dom_anchors
    §CORE-11/web/split                → test_static_styles_define_split_panes
                                      → test_timeline_js_exposes_paneset
    §CORE-11/web/live-rail            → test_livebody_has_chrono_rail_styles
                                      → test_livetail_js_exposes_cdf_sync_api
    §CORE-11/web/api/scripts          → test_web_api_scripts_returns_cards
    §CORE-11/web/api/sessions         → test_web_api_sessions_returns_pool
    §CORE-11/web/api/runs             → test_web_api_runs_reflects_workspace
    §CORE-11/web/api/start-run        → test_web_api_start_run_enqueues

本文件走 FastAPI `TestClient`，只锁 HTTP 契约与静态资源的可获取性。**布局与交互
断言不在这里**：视口锁定、隐藏面板占位、LogRow 响应式列宽已交给
`tests/e2e/`（`§CORE-11/web/e2e/...`）在真实浏览器里量，字符串检查证明不了布局
成立。剩下的 `styles.css` / `timeline.js` 文本断言只是"这条规则别被删掉"的护栏，
待相应 e2e 覆盖到位后同样让位。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import redpymake as rpm

# 需要 [web] extra；缺失时跳过整个文件
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "rpm_hello.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("hello"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('WEB-HELLO')")
""",
    )
    _write(tmp_path, "rpm_bare.py", "def main():\n    pass\n")
    return tmp_path


@pytest.fixture
def web_app(workspace_root: Path):
    from redpymake._web.server import build_app

    with rpm.workspace(workspace_root) as ws:
        app = build_app(ws)
        with TestClient(app) as client:
            yield client, ws


def test_web_index_returns_html(web_app):
    """§CORE-11/web/routes/index：GET / 返回带侧栏三段的 HTML。"""
    client, _ws = web_app
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    # 三段侧栏关键锚
    for anchor in ("Sessions", "Scripts", "Runs"):
        assert anchor in body


def test_web_index_exposes_named_dom_anchors(web_app):
    """§CORE-11/web/dom-names：index 带 AppShell 类与各部件的稳定 DOM 锚点。"""
    client, _ws = web_app
    body = client.get("/").text
    assert 'class="app-shell"' in body, "AppShell 类是视口锁定样式的作用域锚点"
    for anchor in (
        'id="log-switcher"',    # LogSwitcher
        'id="sess-list"',       # SessionList
        'id="scripts-list"',    # ScriptList
        'id="runs-list"',       # RunList
        'id="main-toolbar"',    # MainToolbar
        'id="back-to-live"',    # BackToLiveButton
        'id="live-root"',       # LiveView
        'id="timeline-root"',   # RunDetailView
        'id="split-toggle"',    # SplitToggle
    ):
        assert anchor in body, f"缺少命名部件锚点 {anchor}"


def test_static_styles_are_served(web_app):
    """§CORE-11/web/routes/index：index 引用的两份静态资源确实可取。

    布局本身由 `tests/e2e/test_e2e_app_shell.py` 在浏览器里验；这里只保证 wheel
    里内联的静态资源挂载正确（§CORE-11「前端资源内联进 wheel，不走 CDN」）。
    """
    client, _ws = web_app
    for path in ("/static/styles.css", "/static/timeline.js"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} 未挂载"
        assert res.text.strip()


def test_static_styles_define_split_panes(web_app):
    """§CORE-11/web/split：分栏容器与 Script 兜底列有对应样式。"""
    client, _ws = web_app
    css = client.get("/static/styles.css").text
    for selector in (".pane-set", ".pane-set.split", ".pane-header", ".pane-script"):
        assert selector in css, f"缺少分栏样式 {selector}"


def test_timeline_js_exposes_paneset(web_app):
    """§CORE-11/web/split：PaneSet 是 Live 与 Run detail 共用的落地容器。"""
    client, _ws = web_app
    js = client.get("/static/timeline.js").text
    assert "function PaneSet(" in js
    assert "SCRIPT_PANE_ID" in js
    # 两态都能换列；Timeline 换列不重建实例 → 播放头只有一个
    assert "Timeline.prototype.setPanes" in js
    assert "LiveTail.prototype.setPanes" in js


def test_livebody_has_chrono_rail_styles(web_app):
    """§CORE-11/web/live-rail：LiveBody 每列有 ChronoRail、tick 与 sync 命中行样式。"""
    client, _ws = web_app
    css = client.get("/static/styles.css").text
    for selector in (".pane-content", ".tl-chrono-rail", ".tl-tick", ".tl-sync-target"):
        assert selector in css, f"缺少 ChronoRail 样式 {selector}"
    # tick 靠绝对定位摆 y；rail 必须是它的定位上下文，否则全挤到页面左上角
    rail_block = css.split(".tl-chrono-rail {", 1)[1].split("}", 1)[0]
    assert "position: relative" in rail_block


def test_livetail_js_exposes_cdf_sync_api(web_app):
    """§CORE-11/web/live-rail：LiveTail 暴露 CDF rail 与跨列 snap sync 的入口。"""
    client, _ws = web_app
    js = client.get("/static/timeline.js").text
    for token in (
        "_anchorEvents",           # 全局有序 anchor 表：rail y 与 sync 的唯一坐标系
        "_recordAnchor",
        "_redrawRails",
        "_syncToTs",
        "_nearestRowByTs",
        "SYNC_VIEWPORT_RATIO",
        "ANCHORS_MAX",
        "tl-chrono-rail",
    ):
        assert token in js, f"缺少 ChronoRail 符号 {token}"
    # rail 只给 LiveTail 开；Run detail 已经有 playhead，不建第二根时间轴
    assert "rail: true" in js


def test_web_api_scripts_returns_cards(web_app):
    """§CORE-11/web/api/scripts：/api/scripts 返回发现的 rpm_*.py。"""
    client, _ws = web_app
    res = client.get("/api/scripts")
    assert res.status_code == 200
    data = res.json()
    names = {item["path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for item in data}
    assert {"rpm_hello.py", "rpm_bare.py"} <= names


def test_web_api_sessions_returns_pool(web_app):
    """§CORE-11/web/api/sessions：初始为空；借出后有一项。"""
    client, ws = web_app
    r0 = client.get("/api/sessions")
    assert r0.status_code == 200
    assert r0.json() == []
    # 借出一个会话
    ws.local()
    r1 = client.get("/api/sessions")
    data = r1.json()
    assert len(data) == 1
    assert data[0]["kind"] == "local"


def test_web_api_runs_reflects_workspace(web_app, workspace_root: Path):
    """§CORE-11/web/api/runs：跑一个脚本后 /api/runs 能看到它。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_hello.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
    r = client.get("/api/runs")
    data = r.json()
    ids = [item["id"] for item in data]
    assert rid in ids


def test_web_api_start_run_enqueues(web_app, workspace_root: Path):
    """§CORE-11/web/api/start-run：POST /api/runs 触发 enqueue。"""
    client, ws = web_app
    r = client.post("/api/runs", json={"path": str(workspace_root / "rpm_hello.py")})
    assert r.status_code in (200, 201)
    data = r.json()
    assert "run_id" in data
    rid = data["run_id"]
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
