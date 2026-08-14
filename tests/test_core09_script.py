"""CORE-09 脚本对象与日志分流（doc/core-lib-requirements.md § CORE-09）。

规格映射：
    §CORE-09/factory/callable            → test_script_factory_returns_context_manager
    §CORE-09/factory/type                → test_script_run_type_exposed
    §CORE-09/bridge/logging-to-merged    → test_script_context_bridges_logging_into_merged
    §CORE-09/register/auto               → test_script_captures_sessions_auto_registered
    §CORE-09/register/at-view            → test_script_does_not_double_register_at_view
    §CORE-09/register/explicit-attach    → test_script_attach_supports_pre_created_session
    §CORE-09/register/dedup              → test_script_attach_is_idempotent
    §CORE-09/register/detach             → test_script_detach_stops_forwarding
    §CORE-09/logging/level-gate          → test_script_log_level_gate
    §CORE-09/logging/opt-in-loggers      → test_script_named_loggers_opt_in
    §CORE-09/dump/none                   → test_script_no_dump_when_ok
    §CORE-09/dump/single-file            → test_script_dump_single_file_on_error
    §CORE-09/dump/bundle-dir             → test_script_dump_bundle_on_error
    §CORE-09/dump/callable-sink          → test_script_dump_callable_sink
    §CORE-09/dump/exception-not-swallowed → test_script_dump_does_not_swallow_exception
    §CORE-09/dump/failure-not-masking    → test_script_dump_failure_does_not_mask_original
    §CORE-09/lifecycle/handler-removed   → test_script_handler_removed_on_exit
    §CORE-09/lifecycle/contextvar-reset  → test_script_contextvar_reset_after_exit
    §CORE-09/lifecycle/nested-isolated   → test_script_nested_scopes_isolated
    §CORE-09/snapshot/sorted             → test_script_snapshot_sorted_by_timestamp
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

import redpymake as rpm


# --------------------------------------------------------------------- 工厂/类型


def test_script_factory_returns_context_manager():
    """§CORE-09/factory：``rpm.script(...)`` 返回一个可作上下文管理器的对象。"""
    run = rpm.script(name="probe")
    assert hasattr(run, "__enter__") and hasattr(run, "__exit__")
    with run as ctx:
        assert ctx is run


def test_script_run_type_exposed():
    """§CORE-09/factory/type：``rpm.ScriptRun`` / ``rpm.ScriptSnapshot`` 类型可用。"""
    assert isinstance(rpm.ScriptRun, type)
    assert isinstance(rpm.ScriptSnapshot, type)


# --------------------------------------------------------------------- 桥


def test_script_context_bridges_logging_into_merged(caplog):
    """§CORE-09/bridge/logging-to-merged：``logging.getLogger().info(...)`` 进入 merged。

    需要先把目标 logger 提到 DEBUG，否则默认 root 级别 WARNING 会在 logger
    自身层面拦掉 ``.info(...)`` 记录，永远到不了 ScriptRun 的 handler。
    """
    caplog.set_level(logging.DEBUG, logger="rpm.core09.bridge")
    logger = logging.getLogger("rpm.core09.bridge")
    with rpm.script(name="bridge_probe") as run:
        logger.info("hello-bridge")
    snap = run.snapshot()
    hits = [r for r in snap.records if r.event == "user_log" and "hello-bridge" in r.message]
    assert hits, f"expected user_log record, got: {[r.event for r in snap.records]}"
    hit = hits[0]
    assert hit.stream == "python"
    assert hit.level == "INFO"
    assert hit.fields.get("logger") == "rpm.core09.bridge"


# ----------------------------------------------------------------- Session 登记


def test_script_captures_sessions_auto_registered(python_probe):
    """§CORE-09/register/auto：``with rpm.script():`` 内构造的 root Session 自动登记。"""
    with rpm.script(name="auto_reg") as run:
        with rpm.local() as local:
            local.run(*python_probe("print('AR')"))
    snap = run.snapshot()
    outs = [
        r
        for r in snap.records
        if r.event == "command_output" and r.stream == "stdout" and "AR" in r.message
    ]
    assert outs, "expected local command output in script snapshot"
    assert any(s.kind == "local" for s in snap.sessions)


def test_script_does_not_double_register_at_view(python_probe, tmp_path: Path):
    """§CORE-09/register/at-view：``at()`` 视图共享 root buffer，脚本不重复登记。"""
    with rpm.script(name="view_dedup") as run:
        with rpm.local(default_cwd=str(tmp_path)) as sess:
            view = sess.at(".")
            view.run(*python_probe("print('VD')"))
    snap = run.snapshot()
    outs = [r for r in snap.records if r.event == "command_output" and "VD" in r.message]
    assert len(outs) == 1, f"got {len(outs)} duplicate records; snap events: {[r.event for r in snap.records]}"
    assert len(snap.sessions) == 1


def test_script_attach_supports_pre_created_session(python_probe):
    """§CORE-09/register/explicit-attach：``run.attach(sess)`` 补登预先创建的 session。"""
    sess = rpm.local()
    try:
        with rpm.script(name="explicit_attach") as run:
            run.attach(sess)
            sess.run(*python_probe("print('EX')"))
    finally:
        sess.close()
    snap = run.snapshot()
    hits = [r for r in snap.records if r.event == "command_output" and "EX" in r.message]
    assert hits, "expected pre-created session output after attach"


def test_script_attach_is_idempotent(python_probe):
    """§CORE-09/register/dedup：同一 session 重复 attach 不会产生重复记录。"""
    with rpm.script(name="dedup") as run:
        with rpm.local() as sess:
            run.attach(sess)
            run.attach(sess)
            sess.run(*python_probe("print('DD')"))
    snap = run.snapshot()
    outs = [r for r in snap.records if r.event == "command_output" and "DD" in r.message]
    assert len(outs) == 1


def test_script_detach_stops_forwarding(python_probe):
    """§CORE-09/register/detach：``run.detach(sess)`` 后 session 的新记录不再进 merged。"""
    with rpm.script(name="detach_probe") as run:
        with rpm.local() as sess:
            sess.run(*python_probe("print('BEFORE')"))
            run.detach(sess)
            sess.run(*python_probe("print('AFTER')"))
    snap = run.snapshot()
    msgs = [r.message for r in snap.records if r.event == "command_output"]
    assert any("BEFORE" in m for m in msgs)
    assert not any("AFTER" in m for m in msgs)


# --------------------------------------------------------------------- logging 门槛


def test_script_log_level_gate(caplog):
    """§CORE-09/logging/level-gate：``log_level="WARNING"`` 时 INFO 不进 merged。

    先把目标 logger 提到 DEBUG，确保 INFO 记录不是被 logger 自身的默认 WARNING
    级别拦掉，而是被 ScriptRun handler 上的 ``log_level`` 门槛拦掉；这样断言才
    真正反映本条契约。
    """
    caplog.set_level(logging.DEBUG, logger="rpm.core09.gate")
    logger = logging.getLogger("rpm.core09.gate")
    with rpm.script(name="gate", log_level="WARNING") as run:
        logger.info("skip-me")
        logger.warning("keep-me")
    msgs = [r.message for r in run.snapshot().records if r.event == "user_log"]
    assert not any("skip-me" in m for m in msgs)
    assert any("keep-me" in m for m in msgs)


def test_script_named_loggers_opt_in():
    """§CORE-09/logging/opt-in-loggers：``loggers=["myapp"]`` 时其他 logger 的输出不进 merged。"""
    other = logging.getLogger("otherlib.core09")
    mine = logging.getLogger("myapp.core09")
    with rpm.script(name="opt_in", loggers=["myapp.core09"]) as run:
        other.warning("noise-from-third-party")
        mine.warning("signal-from-my-app")
    msgs = [r.message for r in run.snapshot().records if r.event == "user_log"]
    assert not any("noise-from-third-party" in m for m in msgs)
    assert any("signal-from-my-app" in m for m in msgs)


# --------------------------------------------------------------------- 落盘


def test_script_no_dump_when_ok(tmp_path: Path):
    """§CORE-09/dump/none：正常退出不写盘。"""
    target = tmp_path / "should-not-exist.log"
    with rpm.script(name="ok_no_dump", dump_on_error=str(target)):
        pass
    assert not target.exists()


def test_script_dump_single_file_on_error(tmp_path: Path, python_probe):
    """§CORE-09/dump/single-file：``dump_on_error="...log"`` → 单文件落盘含全量记录 + 异常摘要。"""
    target = tmp_path / "sub" / "run.log"
    logger = logging.getLogger("rpm.core09.singlefile")
    with pytest.raises(RuntimeError, match="boom-single"):
        with rpm.script(name="single", dump_on_error=str(target)) as _:
            logger.warning("USER-LINE")
            with rpm.local() as local:
                local.run(*python_probe("print('CMD-LINE')"))
            raise RuntimeError("boom-single")
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "USER-LINE" in content
    assert "CMD-LINE" in content
    assert "RuntimeError" in content
    assert "boom-single" in content


def test_script_dump_bundle_on_error(tmp_path: Path, python_probe):
    """§CORE-09/dump/bundle-dir：``dump_on_error=<dir>`` → 生成子目录，含 all/script/per-session/meta.json。"""
    logger = logging.getLogger("rpm.core09.bundle")
    with pytest.raises(RuntimeError, match="boom-bundle"):
        with rpm.script(name="bundle", dump_on_error=str(tmp_path) + os.sep) as _:
            logger.warning("BUNDLE-USER")
            with rpm.local() as local:
                local.run(*python_probe("print('BUNDLE-CMD')"))
            raise RuntimeError("boom-bundle")
    subdirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("bundle-")]
    assert len(subdirs) == 1, f"expected 1 bundle dir, got {[p.name for p in tmp_path.iterdir()]}"
    bundle = subdirs[0]
    all_log = (bundle / "all.log").read_text(encoding="utf-8")
    script_log = (bundle / "script.log").read_text(encoding="utf-8")
    assert "BUNDLE-USER" in all_log and "BUNDLE-CMD" in all_log
    assert "BUNDLE-USER" in script_log
    assert "BUNDLE-CMD" not in script_log
    # 至少一份 per-session 日志（本地会话）
    per_session = [
        p for p in bundle.iterdir() if p.suffix == ".log" and p.name not in {"all.log", "script.log"}
    ]
    assert per_session, "expected at least one per-session log file"
    assert any("BUNDLE-CMD" in p.read_text(encoding="utf-8") for p in per_session)
    meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "bundle"
    assert meta["exception"]["type"] == "RuntimeError"
    assert "boom-bundle" in meta["exception"]["message"]
    assert isinstance(meta["sessions"], list) and len(meta["sessions"]) >= 1


def test_script_dump_callable_sink(python_probe):
    """§CORE-09/dump/callable-sink：``dump_on_error=<callable>`` → sink 被调用一次，收到含异常的 snapshot。"""
    captured: list[rpm.ScriptSnapshot] = []

    def _sink(snap: rpm.ScriptSnapshot) -> None:
        captured.append(snap)

    with pytest.raises(ValueError, match="boom-sink"):
        with rpm.script(name="sink", dump_on_error=_sink) as _:
            with rpm.local() as local:
                local.run(*python_probe("print('SINK-CMD')"))
            raise ValueError("boom-sink")

    assert len(captured) == 1
    snap = captured[0]
    assert snap.exception is not None
    assert snap.exception.type == "ValueError"
    assert "boom-sink" in snap.exception.message
    assert any("SINK-CMD" in r.message for r in snap.records)


def test_script_dump_does_not_swallow_exception(tmp_path: Path):
    """§CORE-09/dump/exception-not-swallowed：写盘后异常仍向外传播。"""
    target = tmp_path / "keep-raising.log"
    with pytest.raises(KeyError, match="propagates"):
        with rpm.script(dump_on_error=str(target)):
            raise KeyError("propagates")
    assert target.exists()


def test_script_dump_failure_does_not_mask_original():
    """§CORE-09/dump/failure-not-masking：sink 自身抛错不能掩盖脚本原异常。"""
    def _bad_sink(snap: rpm.ScriptSnapshot) -> None:
        raise IOError("disk-full")

    with pytest.raises(RuntimeError, match="original-exc"):
        with rpm.script(dump_on_error=_bad_sink):
            raise RuntimeError("original-exc")


# --------------------------------------------------------------------- 生命周期


def test_script_handler_removed_on_exit():
    """§CORE-09/lifecycle/handler-removed：退出后 root logger 的 handler 数不增加。"""
    root = logging.getLogger()
    before = list(root.handlers)
    with rpm.script(name="handler_cleanup"):
        assert len(root.handlers) == len(before) + 1
    assert list(root.handlers) == before


def test_script_contextvar_reset_after_exit(python_probe):
    """§CORE-09/lifecycle/contextvar-reset：脚本退出后新 Session 不再登记到旧 run。"""
    with rpm.script(name="cv_a") as run_a:
        pass
    # 出块之后新造的会话不应进 run_a 的 snapshot
    with rpm.local() as sess:
        sess.run(*python_probe("print('OUTSIDE')"))
    assert not any(
        r.event == "command_output" and "OUTSIDE" in r.message for r in run_a.snapshot().records
    )


def test_script_nested_scopes_isolated(python_probe):
    """§CORE-09/lifecycle/nested-isolated：嵌套 script 内层 Session 只进内层。"""
    with rpm.script(name="outer") as outer:
        with rpm.local() as outer_sess:
            outer_sess.run(*python_probe("print('OUTER-CMD')"))
        with rpm.script(name="inner") as inner:
            with rpm.local() as inner_sess:
                inner_sess.run(*python_probe("print('INNER-CMD')"))
        # 内层退出后外层继续采集
        with rpm.local() as after_sess:
            after_sess.run(*python_probe("print('AFTER-INNER')"))

    inner_msgs = [r.message for r in inner.snapshot().records if r.event == "command_output"]
    outer_msgs = [r.message for r in outer.snapshot().records if r.event == "command_output"]

    assert any("INNER-CMD" in m for m in inner_msgs)
    assert not any("OUTER-CMD" in m for m in inner_msgs)
    assert not any("AFTER-INNER" in m for m in inner_msgs)

    assert any("OUTER-CMD" in m for m in outer_msgs)
    assert any("AFTER-INNER" in m for m in outer_msgs)
    assert not any("INNER-CMD" in m for m in outer_msgs)


def test_script_snapshot_sorted_by_timestamp(caplog):
    """§CORE-09/snapshot/sorted：snapshot.records 按 timestamp 稳定排序。"""
    caplog.set_level(logging.DEBUG, logger="rpm.core09.sorted")
    logger = logging.getLogger("rpm.core09.sorted")
    with rpm.script(name="sorted_probe") as run:
        for i in range(5):
            logger.info(f"line-{i}")
    recs = run.snapshot().records
    assert list(recs) == sorted(recs, key=lambda r: (r.timestamp, r.session_id, r.sequence))
