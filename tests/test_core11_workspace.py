"""CORE-11 Workspace 契约（doc/core-lib-requirements.md § CORE-11）。

规格映射：
    §CORE-11/factory                    → test_workspace_factory_returns_context_manager
    §CORE-11/pool/lazy-cache            → test_workspace_local_is_cached_by_key
    §CORE-11/pool/close-all-on-exit     → test_workspace_closes_sessions_on_exit
    §CORE-11/borrow/no-close-on-with-exit→ test_borrowed_session_survives_with_block
    §CORE-11/borrow/factory-integration → test_factory_borrows_when_workspace_active
    §CORE-11/borrow/no-workspace        → test_factory_returns_owned_session_without_workspace
    §CORE-11/queue/enqueue-runs-serial  → test_workspace_enqueue_runs_scripts_serially
    §CORE-11/queue/reload-fresh-code    → test_workspace_reloads_script_on_rerun
    §CORE-11/queue/missing-main         → test_workspace_run_fails_when_main_missing
    §CORE-11/queue/exception-captured   → test_workspace_captures_script_exception
    §CORE-11/queue/ndjson-shared-stream → test_workspace_writes_ndjson_per_run
    §CORE-11/queue/stop-cooperative     → test_workspace_stop_current_marks_run_cancelled
    §CORE-11/queue/stop-idle            → test_workspace_stop_current_is_noop_when_idle
    §CORE-11/queue/rerun                → test_workspace_rerun_creates_new_run
                                        → test_workspace_rerun_unknown_run_raises
    §CORE-11/queue/cancel-queued        → test_workspace_cancel_queued_run
                                        → test_workspace_cancel_running_run_is_rejected
    §CORE-11/discover                   → test_workspace_discover_lists_scripts
    §CORE-11/current-run                → test_workspace_current_run_reflects_state
    §CORE-11/subscribe/run-record       → test_workspace_subscribers_receive_run_record_events
    §CORE-11/subscribe/run-status       → test_workspace_subscribers_receive_run_status_events
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import redpymake as rpm


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


# ------------------------------------------------------------ 基础与生命周期


def test_workspace_factory_returns_context_manager(tmp_path: Path):
    """§CORE-11/factory：``rpm.workspace(root)`` 返回可作上下文管理器的对象。"""
    ws = rpm.workspace(tmp_path)
    assert hasattr(ws, "__enter__") and hasattr(ws, "__exit__")
    with ws as ctx:
        assert ctx is ws


def test_workspace_local_is_cached_by_key(tmp_path: Path):
    """§CORE-11/pool/lazy-cache：``ws.local()`` 多次调用返回同一底层会话。"""
    with rpm.workspace(tmp_path) as ws:
        a = ws.local()
        b = ws.local()
        # 借出代理透传：底层 session_id 一致
        assert a.session_id == b.session_id


def test_workspace_closes_sessions_on_exit(tmp_path: Path):
    """§CORE-11/pool/close-all-on-exit：默认 auto_close_sessions 关掉池内所有会话。"""
    with rpm.workspace(tmp_path) as ws:
        sess = ws.local()
        # 记住底层 root
        root = sess.root
    assert root.closed is True


# ------------------------------------------------------------ 借出语义


def test_borrowed_session_survives_with_block(tmp_path: Path, python_probe):
    """§CORE-11/borrow/no-close-on-with-exit：``with borrowed:`` 出块不 close 真会话。"""
    with rpm.workspace(tmp_path) as ws:
        sess = ws.local()
        root = sess.root
        with sess:
            sess.run(*python_probe("print('hi')"))
        # 出 with 块，会话仍活着（Workspace 拥有）
        assert root.closed is False
        # 再次借出 + 使用仍可
        sess2 = ws.local()
        sess2.run(*python_probe("print('again')"))


def test_factory_borrows_when_workspace_active(tmp_path: Path, python_probe):
    """§CORE-11/borrow/factory-integration：workspace 作用域内 ``rpm.local()`` 自动借出。"""
    with rpm.workspace(tmp_path) as ws:
        first = ws.local()
        # 在 workspace 生命周期内直接用顶层工厂
        borrowed = rpm.local()
        assert borrowed.root is first.root, "rpm.local() should reuse workspace session"


def test_factory_returns_owned_session_without_workspace(python_probe):
    """§CORE-11/borrow/no-workspace：workspace 外，``rpm.local()`` 依旧是独立会话。"""
    a = rpm.local()
    b = rpm.local()
    try:
        assert a.session_id != b.session_id
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------ 队列执行


def test_workspace_enqueue_runs_scripts_serially(tmp_path: Path):
    """§CORE-11/queue/enqueue-runs-serial：多脚本按 FIFO 串行执行，均达到 succeeded。"""
    _write(
        tmp_path,
        "rpm_a.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("a"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('A')")
""",
    )
    _write(
        tmp_path,
        "rpm_b.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("b"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('B')")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        id_a = ws.enqueue(tmp_path / "rpm_a.py")
        id_b = ws.enqueue(tmp_path / "rpm_b.py")
        # 等两个都跑完
        ok = _wait_until(
            lambda: all(
                ws.get_run(r).status in {"succeeded", "failed"} for r in (id_a, id_b)
            ),
            timeout=15,
        )
        assert ok, [ws.get_run(id_a).status, ws.get_run(id_b).status]
        assert ws.get_run(id_a).status == "succeeded"
        assert ws.get_run(id_b).status == "succeeded"


def test_workspace_reloads_script_on_rerun(tmp_path: Path):
    """§CORE-11/queue/reload-fresh-code：修改脚本后再次 enqueue，新代码生效。"""
    script = _write(
        tmp_path,
        "rpm_reload.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("r"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('V1')")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid1 = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid1).status == "succeeded", timeout=10)
        recs1 = list(ws.iter_run_records(rid1))
        assert any("V1" in r.get("message", "") for r in recs1)

        # 改脚本
        script.write_text(
            """import redpymake as rpm

def main() -> None:
    with rpm.script("r"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('V2')")
""",
            encoding="utf-8",
        )
        rid2 = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid2).status == "succeeded", timeout=10)
        recs2 = list(ws.iter_run_records(rid2))
        assert any("V2" in r.get("message", "") for r in recs2)
        assert not any("V1" in r.get("message", "") for r in recs2)


def test_workspace_run_fails_when_main_missing(tmp_path: Path):
    """§CORE-11/queue/missing-main：脚本没有 def main() 时 run 状态为 failed。"""
    script = _write(tmp_path, "rpm_nomain.py", "x = 1  # no main()\n")
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "failed", timeout=10)
        assert ws.get_run(rid).exception is not None


def test_workspace_captures_script_exception(tmp_path: Path):
    """§CORE-11/queue/exception-captured：脚本抛异常不搞挂 Workspace；run 记为 failed。"""
    script = _write(
        tmp_path,
        "rpm_boom.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("boom"):
        raise RuntimeError("scripted boom")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "failed", timeout=10)
        run = ws.get_run(rid)
        assert "RuntimeError" in (run.exception or "")
        # Workspace 仍存活；能继续跑
        script2 = _write(
            tmp_path,
            "rpm_ok.py",
            """import redpymake as rpm

def main() -> None:
    with rpm.script("ok"):
        pass
""",
        )
        rid2 = ws.enqueue(script2)
        assert _wait_until(lambda: ws.get_run(rid2).status == "succeeded", timeout=10)


def test_workspace_writes_ndjson_per_run(tmp_path: Path):
    """§CORE-11/queue/ndjson-shared-stream：run 的记录落进所属日志的共享 stream。

    日志组模型下 ``ndjson_path`` 指向该 log 唯一的 ``stream.ndjson``（多 run 共享），
    单个 run 的记录要通过 ``iter_run_records`` 按 begin/end 元行切片拿。
    """
    script = _write(
        tmp_path,
        "rpm_nd.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("nd"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('ND-LINE')")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=10)
        run = ws.get_run(rid)
        assert run.ndjson_path.exists()
        assert run.ndjson_path.name == "stream.ndjson"
        assert run.ndjson_path == ws.current_log.stream_path

        records = list(ws.iter_run_records(rid))
        events = [r.get("event") for r in records]
        # 区间由 workspace 元行界定，内部夹着 CORE-10 sink 落的内容
        assert events[0] == "workspace.run.begin"
        assert events[-1] == "workspace.run.end"
        assert "script.begin" in events
        assert "script.end" in events
        assert any("ND-LINE" in (r.get("message") or "") for r in records)


def test_workspace_stop_current_marks_run_cancelled(tmp_path: Path):
    """§CORE-11/queue/stop-cooperative：``stop_current`` 让脚本在下一条命令边界终止。

    停止是协作式的：不强杀在跑的子进程，而是让借出会话的下一次 ``run()`` 抛
    ``WorkspaceStoppedError``；run 终态记为 cancelled。
    """
    script = _write(
        tmp_path,
        "rpm_stop.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("stoppable"):
        with rpm.local() as sess:
            for i in range(40):
                sess.run("python", "-c", "print('TICK')")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        # 等它真的跑起来，再请求停止
        assert _wait_until(lambda: ws.get_run(rid).status == "running", timeout=10)
        assert ws.stop_current() is True
        assert _wait_until(
            lambda: ws.get_run(rid).status not in {"queued", "running"}, timeout=15
        )
        run = ws.get_run(rid)
        assert run.status == "cancelled", run.status
        # 停止后队列仍可用
        assert ws.current_run is None


def test_workspace_stop_current_is_noop_when_idle(tmp_path: Path):
    """§CORE-11/queue/stop-idle：空闲时 ``stop_current`` 返回 False，不影响后续 run。"""
    script = _write(
        tmp_path,
        "rpm_idle.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("idle"):
        pass
""",
    )
    with rpm.workspace(tmp_path) as ws:
        assert ws.stop_current() is False
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=10)


def test_workspace_rerun_creates_new_run(tmp_path: Path):
    """§CORE-11/queue/rerun：``rerun(rid)`` 用同一脚本新起一个 run，原记录不动。"""
    script = _write(
        tmp_path,
        "rpm_again.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("again"):
        pass
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid1 = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid1).status == "succeeded", timeout=10)

        rid2 = ws.rerun(rid1)
        assert rid2 != rid1
        assert _wait_until(lambda: ws.get_run(rid2).status == "succeeded", timeout=10)
        assert ws.get_run(rid2).script_path == ws.get_run(rid1).script_path
        # 原 run 保持终态，没被覆盖
        assert ws.get_run(rid1).status == "succeeded"
        assert {rid1, rid2} <= {r.id for r in ws.runs}


def test_workspace_rerun_unknown_run_raises(tmp_path: Path):
    """§CORE-11/queue/rerun：未知 run_id 抛 KeyError，供 Web 层翻成 404。"""
    with rpm.workspace(tmp_path) as ws:
        with pytest.raises(KeyError):
            ws.rerun("nope#999")


def test_workspace_cancel_queued_run(tmp_path: Path):
    """§CORE-11/queue/cancel-queued：排队中的 run 可被剔出队列并标 cancelled。

    被取消的 run 从未开跑，因此不该在 stream 里留下 ``workspace.run.begin``。
    """
    slow = _write(
        tmp_path,
        "rpm_slow.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("slow"):
        with rpm.local() as sess:
            sess.run("python", "-c", "import time; time.sleep(1.5)")
""",
    )
    later = _write(
        tmp_path,
        "rpm_later.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("later"):
        pass
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid_slow = ws.enqueue(slow)
        rid_later = ws.enqueue(later)
        # 第一个占住 worker，第二个还在队列里
        assert _wait_until(lambda: ws.get_run(rid_slow).status == "running", timeout=10)
        assert ws.get_run(rid_later).status == "queued"

        assert ws.cancel_run(rid_later) is True
        assert ws.get_run(rid_later).status == "cancelled"
        # 已经取消过 → 再取消返回 False
        assert ws.cancel_run(rid_later) is False

        assert _wait_until(lambda: ws.get_run(rid_slow).status == "succeeded", timeout=15)
        # 被取消的 run 没进过 stream
        stream_text = ws.current_log.stream_path.read_text(encoding="utf-8")
        assert rid_slow in stream_text
        assert rid_later not in stream_text


def test_workspace_cancel_running_run_is_rejected(tmp_path: Path):
    """§CORE-11/queue/cancel-queued：``cancel_run`` 只管队列；已在跑的返回 False。"""
    script = _write(
        tmp_path,
        "rpm_busy.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("busy"):
        with rpm.local() as sess:
            sess.run("python", "-c", "import time; time.sleep(1.0)")
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "running", timeout=10)
        assert ws.cancel_run(rid) is False
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)


# ------------------------------------------------------------ 内省


def test_workspace_discover_lists_scripts(tmp_path: Path):
    """§CORE-11/discover：``ws.discover()`` 委托到 CORE-10。"""
    _write(tmp_path, "rpm_a.py", "def main():\n    pass\n")
    _write(tmp_path, "rpm_b.py", "def main():\n    pass\n")
    with rpm.workspace(tmp_path) as ws:
        cards = ws.discover()
        assert sorted(c.path.name for c in cards) == ["rpm_a.py", "rpm_b.py"]


def test_workspace_current_run_reflects_state(tmp_path: Path):
    """§CORE-11/current-run：``ws.current_run`` 在跑完后回落到 None，历史留在 ws.runs 里。"""
    script = _write(
        tmp_path,
        "rpm_cr.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("cr"):
        pass
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=10)
        # 空闲时 current_run 为 None
        assert _wait_until(lambda: ws.current_run is None, timeout=2)
        ids = [r.id for r in ws.runs]
        assert rid in ids


# ------------------------------------------------------------ 订阅广播


def test_workspace_subscribers_receive_run_record_events(tmp_path: Path):
    """§CORE-11/subscribe/run-record：脚本运行时，subscribe 回调持续收到 ``run.record``。

    这是 Web UI 实时时间轴 & 回放的数据源；即使 ``main()`` 直接抛异常，也应至少
    产出一条命令相关的 record 或 user_log，供浏览器端可视化。
    """
    script = _write(
        tmp_path,
        "rpm_sub.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("sub"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('SUB-LINE')")
""",
    )
    events: list[dict] = []
    with rpm.workspace(tmp_path) as ws:
        ws.subscribe(events.append)
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=10)
        # 允许订阅通知短暂延后
        assert _wait_until(
            lambda: any(
                e.get("type") == "run.record" and e.get("run_id") == rid for e in events
            ),
            timeout=3,
        )
    record_events = [
        e for e in events if e.get("type") == "run.record" and e.get("run_id") == rid
    ]
    assert record_events, "run.record events must be emitted during a run"
    payload_rec = record_events[0].get("record") or {}
    for key in ("timestamp", "sequence", "session_id", "event", "level", "message"):
        assert key in payload_rec, f"missing key {key!r} in record payload"
    # 至少有一条 record 的 message 命中 SUB-LINE（会话或命令输出）
    assert any("SUB-LINE" in (e["record"].get("message") or "") for e in record_events)


def test_workspace_subscribers_receive_run_status_events(tmp_path: Path):
    """§CORE-11/subscribe/run-status：run 生命周期事件按顺序广播。"""
    script = _write(
        tmp_path,
        "rpm_status.py",
        """import redpymake as rpm

def main() -> None:
    with rpm.script("st"):
        pass
""",
    )
    events: list[dict] = []
    with rpm.workspace(tmp_path) as ws:
        ws.subscribe(events.append)
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=10)
        assert _wait_until(
            lambda: any(e.get("type") == "run.finished" for e in events), timeout=3
        )
    types = [e.get("type") for e in events if e.get("run_id") == rid]
    assert "run.enqueued" in types
    assert "run.started" in types
    assert "run.finished" in types
    # 顺序：enqueued -> started -> finished
    idx = {t: i for i, t in enumerate(types) if t in {"run.enqueued", "run.started", "run.finished"}}
    assert idx["run.enqueued"] < idx["run.started"] < idx["run.finished"]
