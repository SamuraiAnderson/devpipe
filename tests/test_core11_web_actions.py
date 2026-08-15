"""CORE-11 Web run 操作契约（doc/core-lib-requirements.md § CORE-11 Web UI 交互模型）。

Sidebar Runs 每条按状态呈现 ⏹ / ⟳ 按钮，背后就是这三条路由。本文件锁死它们的
HTTP 语义（成功码、冲突码、状态迁移），前端可以放心按状态码分支。

规格映射：
    §CORE-11/web/api/run-stop      → test_stop_running_run_marks_cancelled
                                   → test_stop_rejects_run_that_is_not_running
                                   → test_stop_unknown_run_is_404
    §CORE-11/web/api/run-rerun     → test_rerun_creates_new_run
                                   → test_rerun_unknown_run_is_404
    §CORE-11/web/api/run-cancel    → test_cancel_queued_run
                                   → test_cancel_rejects_running_run
                                   → test_cancel_unknown_run_is_404
    §CORE-11/web/api/log-records   → test_log_scoped_records_returns_run_slice
                                   → test_log_scoped_records_404_for_foreign_run
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

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


def _wait_until(pred, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _u(run_id: str) -> str:
    """run_id 含 ``#``，直接拼进 URL 会被当成 fragment 截断——必须整段编码。"""
    return quote(run_id, safe="")


_QUICK = """import redpymake as rpm

def main() -> None:
    with rpm.script("quick"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('QUICK')")
"""

# 分成很多条短命令：协作式停止在命令边界生效，边界越多越快停下来
_LONG = """import redpymake as rpm

def main() -> None:
    with rpm.script("long"):
        with rpm.local() as sess:
            for _ in range(60):
                sess.run("python", "-c", "print('TICK')")
"""

_SLOW = """import redpymake as rpm

def main() -> None:
    with rpm.script("slow"):
        with rpm.local() as sess:
            sess.run("python", "-c", "import time; time.sleep(1.5)")
"""


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    _write(tmp_path, "rpm_quick.py", _QUICK)
    _write(tmp_path, "rpm_long.py", _LONG)
    _write(tmp_path, "rpm_slow.py", _SLOW)
    return tmp_path


@pytest.fixture
def web_app(workspace_root: Path):
    from redpymake._web.server import build_app

    with rpm.workspace(workspace_root) as ws:
        app = build_app(ws)
        with TestClient(app) as client:
            yield client, ws


# ------------------------------------------------------------ stop


def test_stop_running_run_marks_cancelled(web_app, workspace_root: Path):
    """§CORE-11/web/api/run-stop：停正在跑的 run，终态为 cancelled。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_long.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "running")

    res = client.post(f"/api/runs/{_u(rid)}/stop")
    assert res.status_code == 200
    assert res.json()["stopped"] is True

    assert _wait_until(lambda: ws.get_run(rid).status not in {"queued", "running"})
    assert ws.get_run(rid).status == "cancelled"


def test_stop_rejects_run_that_is_not_running(web_app, workspace_root: Path):
    """§CORE-11/web/api/run-stop：已经跑完的 run 停不了，返回 409。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_quick.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded")

    res = client.post(f"/api/runs/{_u(rid)}/stop")
    assert res.status_code == 409
    assert ws.get_run(rid).status == "succeeded"


def test_stop_unknown_run_is_404(web_app):
    """§CORE-11/web/api/run-stop：未知 run_id → 404。"""
    client, _ws = web_app
    assert client.post("/api/runs/nope%23999/stop").status_code == 404


# ------------------------------------------------------------ rerun


def test_rerun_creates_new_run(web_app, workspace_root: Path):
    """§CORE-11/web/api/run-rerun：返回 201 + 新 run_id，原 run 保持终态。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_quick.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded")

    res = client.post(f"/api/runs/{_u(rid)}/rerun")
    assert res.status_code == 201
    body = res.json()
    assert body["source_run_id"] == rid
    new_rid = body["run_id"]
    assert new_rid != rid

    assert _wait_until(lambda: ws.get_run(new_rid).status == "succeeded")
    assert ws.get_run(new_rid).script_path == ws.get_run(rid).script_path
    assert ws.get_run(rid).status == "succeeded"


def test_rerun_unknown_run_is_404(web_app):
    """§CORE-11/web/api/run-rerun：未知 run_id → 404。"""
    client, _ws = web_app
    assert client.post("/api/runs/nope%23999/rerun").status_code == 404


# ------------------------------------------------------------ cancel (queued)


def test_cancel_queued_run(web_app, workspace_root: Path):
    """§CORE-11/web/api/run-cancel：DELETE 把排队中的 run 剔出队列。"""
    client, ws = web_app
    busy = ws.enqueue(workspace_root / "rpm_slow.py")
    queued = ws.enqueue(workspace_root / "rpm_quick.py")
    assert _wait_until(lambda: ws.get_run(busy).status == "running")
    assert ws.get_run(queued).status == "queued"

    res = client.delete(f"/api/runs/{_u(queued)}")
    assert res.status_code == 200
    assert ws.get_run(queued).status == "cancelled"

    # 重复取消 → 409（已经不在队列里了）
    assert client.delete(f"/api/runs/{_u(queued)}").status_code == 409
    assert _wait_until(lambda: ws.get_run(busy).status == "succeeded")


def test_cancel_rejects_running_run(web_app, workspace_root: Path):
    """§CORE-11/web/api/run-cancel：DELETE 只管队列；已在跑的返回 409（该走 stop）。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_slow.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "running")

    assert client.delete(f"/api/runs/{_u(rid)}").status_code == 409
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded")


def test_cancel_unknown_run_is_404(web_app):
    """§CORE-11/web/api/run-cancel：未知 run_id → 404。"""
    client, _ws = web_app
    assert client.delete("/api/runs/nope%23999").status_code == 404


# ------------------------------------------------------------ 历史日志的 run 切片


def test_log_scoped_records_returns_run_slice(web_app, workspace_root: Path):
    """§CORE-11/web/api/log-records：按 log + run 取回该 run 在 stream 里的区间。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_quick.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded")
    lid = ws.current_log.id
    ws.rotate_log()  # 把它变成历史日志，模拟前端浏览旧日志

    res = client.get(f"/api/logs/{lid}/runs/{_u(rid)}/records")
    assert res.status_code == 200
    data = res.json()
    assert data["run"]["id"] == rid
    events = [r["event"] for r in data["records"]]
    assert events[0] == "workspace.run.begin"
    assert events[-1] == "workspace.run.end"
    assert any("QUICK" in (r.get("message") or "") for r in data["records"])


def test_log_scoped_records_404_for_foreign_run(web_app, workspace_root: Path):
    """§CORE-11/web/api/log-records：run 不属于该 log（或 log 不存在）→ 404。"""
    client, ws = web_app
    rid = ws.enqueue(workspace_root / "rpm_quick.py")
    assert _wait_until(lambda: ws.get_run(rid).status == "succeeded")
    lid = ws.current_log.id

    assert client.get(f"/api/logs/{lid}/runs/ghost%231/records").status_code == 404
    assert client.get(f"/api/logs/no-such-log/runs/{_u(rid)}/records").status_code == 404
