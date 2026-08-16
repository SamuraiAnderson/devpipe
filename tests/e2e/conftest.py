"""Web UI 端到端测试专用 fixture（§CORE-11「Web UI 端到端验证（Playwright）」）。

与 ``tests/test_core11_web*.py`` 的分工：那边用 FastAPI ``TestClient`` 锁 HTTP
契约；这里起**真实 uvicorn** 让浏览器连进来，验渲染后果与 WS 推送。``TestClient``
走的是内存传输，浏览器连不上，所以这一层必须监听真实端口。

缺 ``playwright`` / 浏览器二进制时整个目录 skip，不让默认套件因收集失败变红。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest

import redpymake as rpm

if TYPE_CHECKING:  # 运行时不导入：未装 playwright 的机器也要能收集本目录
    from playwright.sync_api import Page

# 缺 e2e extra 时跳过整个目录（skip 而不是 error）
pytest.importorskip(
    "playwright.sync_api",
    reason="需要 e2e extra：pip install 'redpymake[e2e]' && python -m playwright install chromium",
)
pytest.importorskip("pytest_playwright", reason="需要 e2e extra：pip install 'redpymake[e2e]'")
uvicorn = pytest.importorskip("uvicorn", reason="需要 web extra：pip install 'redpymake[web]'")

# 浏览器操作留足余量：chromium 冷启动 + 首屏 fetch 都算在里面
UI_TIMEOUT_MS = 15_000
# 跑一个脚本要起子进程、连本地会话、回推 WS，比纯 UI 操作慢一个量级
RUN_TIMEOUT_MS = 40_000
SERVER_READY_TIMEOUT = 20.0

_HELLO_SCRIPT = """import redpymake as rpm


def main() -> None:
    with rpm.script("hello"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('E2E-HELLO')")
"""


@pytest.fixture(scope="session", autouse=True)
def _require_chromium() -> None:
    """浏览器二进制缺失时给一条可操作的 skip，而不是 pytest-playwright 的原始报错。"""
    from playwright.sync_api import sync_playwright

    hint = "chromium 未安装：先跑 `python -m playwright install chromium`"
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
    except Exception as exc:  # driver 自身跑不起来（node 缺失等）
        pytest.skip(f"playwright driver 不可用（{exc}）；{hint}")
    if not exe or not Path(exe).exists():
        pytest.skip(hint)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """带一个可运行脚本的 workspace 根目录。

    脚本名 ``rpm_hello.py`` 里 ``rpm.script("hello")`` 决定了 ScriptItem 的显示
    标签是 ``hello``（见 ``renderScriptList`` 的 ``c.script_name``），用例按这个
    标签定位 RunAction。
    """
    (tmp_path / "rpm_hello.py").write_text(_HELLO_SCRIPT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def live_server(workspace_root: Path) -> Iterator[tuple[str, rpm.Workspace]]:
    """真实 uvicorn + ``Workspace``；yield ``(base_url, workspace)``。

    uvicorn ≥ 0.29 的 ``capture_signals()`` 自己会跳过非主线程，所以后台线程里
    直接 ``server.run()`` 不会撞上"signal only works in main thread"。
    """
    from redpymake._web.server import build_app

    with rpm.workspace(workspace_root) as ws:
        app = build_app(ws)
        # port=0 + bind_socket()：先拿到已 bind 的 socket 再交给 server，避免
        # "先探空闲端口、再启动"之间的窗口被别的进程抢走端口。
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        sock = config.bind_socket()
        port = sock.getsockname()[1]
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            name=f"e2e-uvicorn-{port}",
            daemon=True,
        )
        thread.start()
        try:
            _await_started(server, thread)
            yield f"http://127.0.0.1:{port}", ws
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture
def app_page(page: "Page", live_server: tuple[str, rpm.Workspace]) -> tuple["Page", rpm.Workspace]:
    """打开主页并等首屏数据到位；返回 ``(page, workspace)``。

    就绪判据用 ``window.__rpmStore``（§CORE-11 约定的测试钩子）而不是等某个 DOM
    节点：``scripts`` 非空说明首批 ``/api/scripts`` 已经回来并进了 store。
    """
    base_url, ws = live_server
    page.set_default_timeout(UI_TIMEOUT_MS)
    page.goto(base_url)
    page.wait_for_function(
        "() => !!(window.__rpmStore && window.__rpmStore.getState().scripts.length)"
    )
    return page, ws


@pytest.fixture
def page_with_log_rows(app_page: tuple["Page", rpm.Workspace]) -> tuple["Page", rpm.Workspace]:
    """跑一次 hello 并等 LiveBody 出现 LogRow，给只关心布局的用例造数据。

    只有需要"页面上有日志行"的布局用例才用它；验证运行流本身的用例自己点按钮，
    以免把被测行为藏进 fixture。
    """
    page, ws = app_page
    page.get_by_title("Run hello").click()
    page.locator("#live-root ol.live-body li.tl-row").first.wait_for(timeout=RUN_TIMEOUT_MS)
    return page, ws


def _await_started(server: "uvicorn.Server", thread: threading.Thread) -> None:
    """等 uvicorn 真正开始监听；线程提前死掉就立刻报错，不要干等到超时。"""
    deadline = time.monotonic() + SERVER_READY_TIMEOUT
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("uvicorn 线程在启动过程中退出了")
        time.sleep(0.02)
    raise RuntimeError(f"uvicorn 在 {SERVER_READY_TIMEOUT}s 内未就绪")
