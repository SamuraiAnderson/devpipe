"""CORE-06 日志收集与等待（doc/core-lib-requirements.md § CORE-06）。

规格映射：
    §CORE-06/auto-collect/stdout-stderr → test_run_auto_collects_stdout_and_stderr
    §CORE-06/auto-collect/operation_id  → test_command_records_share_operation_id
    §CORE-06/auto-collect/command_start → test_command_start_and_end_recorded
    §CORE-06/auto-collect/timeout       → test_timeout_produces_command_end_error
    §CORE-06/records/save/text          → test_logs_save_writes_text
    §CORE-06/records/text/filter-events → test_logs_text_filters_events
    §CORE-06/subscribe/basic            → test_logs_subscribe_receives_records
    §CORE-06/subscribe/unsubscribe      → test_logs_unsubscribe_stops_delivery
    §CORE-06/capacity/ring              → test_log_buffer_ring_capacity
    §CORE-06/capacity/sequence          → test_sequence_increases_monotonically
    §CORE-06/tag/attaches               → test_tag_attaches_fields_within_block
    §CORE-06/tag/nested                 → test_tag_nested_inner_wins
    §CORE-06/tag/thread-safe            → test_tag_isolated_between_threads
    §CORE-06/wait/matches-new-only      → test_wait_default_matches_new_records_only
    §CORE-06/wait/with-cursor           → test_wait_with_explicit_cursor_finds_past
    §CORE-06/wait/regex                 → test_wait_regex_pattern
    §CORE-06/wait/channel-filter        → test_wait_channel_filter
    §CORE-06/wait/times-out             → test_wait_times_out_with_error_fields
    §CORE-06/wait/closed-session       → test_wait_raises_when_session_closes_mid_wait
    §CORE-06/run-wait/no-race           → test_run_wait_no_race
    §CORE-06/wait/or-list               → test_wait_or_list_returns_index
    §CORE-06/wait/or-empty              → test_wait_empty_sequence_raises
    §CORE-06/wait/or-mixed-types        → test_wait_mixed_text_and_bytes_raises
    §CORE-06/wait/multiline             → test_wait_multiline_joins_records
    §CORE-06/wait/chain                 → test_wait_chain_starts_after_match
    §CORE-06/wait/bytes                 → test_wait_bytes_on_serial
    §CORE-06/wait/bytes/regex           → test_wait_bytes_regex
    §CORE-06/wait/bytes/newline         → test_wait_bytes_does_not_split_on_lf
    §CORE-06/wait/bytes/run-no-race     → test_run_wait_bytes_no_race
    §CORE-06/wait/bytes/chain           → test_wait_bytes_chain_starts_after_match
    §CORE-06/wait/bytes/unsupported     → test_local_wait_bytes_unsupported
    §CORE-06/secret-hygiene/env-keys    → test_env_values_not_recorded_in_logs
    §CORE-06/at-view/shares-buffer      → test_at_view_shares_log_buffer
    §CORE-06/closed/still-readable      → test_logs_readable_after_close
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake._logs import LogBuffer
from redpymake.exceptions import (
    CommandTimeoutError,
    LogWaitTimeoutError,
    SessionClosedError,
    UnsupportedOperationError,
)

from ._helpers.log_probe import find_by_op, find_events
from ._helpers.serial_stub import open_stub_serial


# ---------------------------------------------------- 自动收集（真实 run）


def test_run_auto_collects_stdout_and_stderr(local_session, python_probe):
    """§CORE-06：``run()`` 必须把 stdout/stderr 逐行写入日志，且分离流。"""
    cursor = local_session.logs.cursor()
    local_session.run(*python_probe(
        "import sys; print('OUT'); sys.stderr.write('ERR\\n'); sys.stderr.flush()"
    ))
    recs = local_session.logs.records(since=cursor)
    outs = [r for r in recs if r.event == "command_output" and r.stream == "stdout"]
    errs = [r for r in recs if r.event == "command_output" and r.stream == "stderr"]
    assert any("OUT" in r.message for r in outs)
    assert any("ERR" in r.message for r in errs)
    assert not any("ERR" in r.message for r in outs)


def test_command_records_share_operation_id(local_session, python_probe):
    """§CORE-06：一条命令的 start/output/end 使用同一非空 ``operation_id``。"""
    cursor = local_session.logs.cursor()
    local_session.run(*python_probe("print('X')"))
    recs = local_session.logs.records(since=cursor)
    cmd_events = [r for r in recs if r.event.startswith("command_")]
    op_ids = {r.operation_id for r in cmd_events}
    assert None not in op_ids
    assert len(op_ids) == 1


def test_command_start_and_end_recorded(local_session, python_probe):
    """§CORE-06：``run()`` 应写 ``command_start`` 与 ``command_end``。"""
    cursor = local_session.logs.cursor()
    local_session.run(*python_probe("print('Y')"))
    events = [r.event for r in local_session.logs.records(since=cursor)]
    assert "command_start" in events
    assert "command_end" in events


def test_timeout_produces_command_end_error(local_session, python_probe):
    """§CORE-06：超时也必须完整落日志（``command_end`` level=ERROR）。"""
    cursor = local_session.logs.cursor()
    with pytest.raises(CommandTimeoutError):
        local_session.run(
            *python_probe("import time; time.sleep(3)"), timeout=0.2
        )
    recs = local_session.logs.records(since=cursor)
    end = [r for r in recs if r.event == "command_end"]
    assert end and end[-1].level == "ERROR"


# --------------------------------------------------------------- save/text


def test_logs_save_writes_text(local_session, python_probe, tmp_path: Path):
    """§CORE-06：``logs.save`` 把命令输出文本写入文件；父目录自动创建。"""
    local_session.run(*python_probe("print('alpha')"))
    local_session.run(*python_probe("print('beta')"))
    out = tmp_path / "sub" / "log.txt"
    local_session.logs.save(str(out))
    content = out.read_text()
    assert "alpha" in content
    assert "beta" in content


def test_logs_text_filters_events(local_session, python_probe):
    """§CORE-06：``logs.text()`` 默认只拼接 ``command_output`` 事件。"""
    local_session.run(*python_probe("print('MSG')"))
    text = local_session.logs.text()
    # command_start 是 "$ ..."；不应出现在 text() 默认输出中
    assert "MSG" in text
    assert "$" not in text.splitlines()[0] if text else True


# ------------------------------------------------------------- subscribe


def test_logs_subscribe_receives_records(local_session, python_probe):
    """§CORE-06：``subscribe`` 注册的回调按顺序收到每条新记录。"""
    received: list[str] = []
    unsub = local_session.logs.subscribe(lambda r: received.append(r.message))
    try:
        local_session.run(*python_probe("print('SUB')"))
    finally:
        unsub()
    assert any("SUB" in m for m in received)


def test_logs_unsubscribe_stops_delivery(local_session, python_probe):
    """§CORE-06：取消订阅后不再收到新记录。"""
    received: list[str] = []
    unsub = local_session.logs.subscribe(lambda r: received.append(r.message))
    unsub()
    local_session.run(*python_probe("print('after-unsubscribe')"))
    assert not any("after-unsubscribe" in m for m in received)


# ------------------------------------------------------------- capacity


def test_log_buffer_ring_capacity():
    """§CORE-06：日志缓冲有容量上限，超过容量按环形丢弃最旧。"""
    buf = LogBuffer("t", capacity=3)
    for i in range(5):
        buf.append(
            event="command_output",
            level="INFO",
            stream="stdout",
            message=str(i),
        )
    assert [r.message for r in buf.records()] == ["2", "3", "4"]


def test_sequence_increases_monotonically():
    """§CORE-06：即便发生裁剪，``sequence`` 仍单调递增。"""
    buf = LogBuffer("t", capacity=3)
    for i in range(5):
        buf.append(
            event="command_output",
            level="INFO",
            stream="stdout",
            message=str(i),
        )
    seqs = [r.sequence for r in buf.records()]
    assert seqs == sorted(seqs)
    assert seqs[0] > 0


# --------------------------------------------------------------------- tag


def test_tag_attaches_fields_within_block(local_session, python_probe):
    """§CORE-06：``logs.tag(**kw)`` 只对 with 块内产生的记录附加字段。"""
    cursor = local_session.logs.cursor()
    with local_session.logs.tag(step="build", branch="main"):
        local_session.run(*python_probe("print('tagged')"))
    local_session.run(*python_probe("print('untagged')"))
    recs = local_session.logs.records(since=cursor)
    tagged = [r for r in recs if r.message == "tagged"]
    untagged = [r for r in recs if r.message == "untagged"]
    assert tagged and tagged[0].fields.get("step") == "build"
    assert tagged[0].fields.get("branch") == "main"
    assert untagged and "step" not in untagged[0].fields


def test_tag_nested_inner_wins(local_session, python_probe):
    """§CORE-06：``tag`` 嵌套时同名键内层覆盖外层。"""
    cursor = local_session.logs.cursor()
    with local_session.logs.tag(step="outer"):
        with local_session.logs.tag(step="inner"):
            local_session.run(*python_probe("print('nested')"))
    rec = next(
        r for r in local_session.logs.records(since=cursor)
        if r.message == "nested"
    )
    assert rec.fields.get("step") == "inner"


def test_tag_isolated_between_threads(local_session):
    """§CORE-06：``tag`` 必须线程/协程安全，不同线程的标签互不串。

    直接使用 ``logs.buffer.append`` 避免 subprocess 干扰，聚焦在 ambient
    tag 的隔离性上。
    """
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(name: str) -> None:
        with local_session.logs.tag(worker=name):
            barrier.wait(timeout=2)
            # 两个线程同时处于各自的 tag() 中
            rec = local_session.logs.buffer.append(
                event="command_output",
                level="INFO",
                stream="stdout",
                message=f"msg-{name}",
            )
            results[name] = rec.fields.get("worker")

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert results == {"A": "A", "B": "B"}


# --------------------------------------------------------------------- wait


def test_wait_default_matches_new_records_only(local_session):
    """§CORE-06：``wait()`` 默认（``since=None``）不匹配调用之前的历史。"""
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="OLD"
    )
    with pytest.raises(LogWaitTimeoutError):
        local_session.wait("OLD", timeout=0.15)


def test_wait_with_explicit_cursor_finds_past(local_session):
    """§CORE-06：``since=cursor`` 可将起点前移到 cursor 之后的所有记录。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="HELLO"
    )
    m = local_session.wait("HELLO", timeout=1, since=cursor)
    assert m.text == "HELLO"


def test_wait_regex_pattern(local_session):
    """§CORE-06：``re.Pattern`` 走正则搜索。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="serial", message="login: "
    )
    m = local_session.wait(re.compile(r"login:\s*$"), timeout=1, since=cursor)
    assert "login:" in m.text


def test_wait_channel_filter(local_session):
    """§CORE-06：``channel`` 参数限制来源；不匹配的流不算命中。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stderr", message="ERR-XYZ"
    )
    with pytest.raises(LogWaitTimeoutError):
        local_session.wait("ERR-XYZ", timeout=0.15, channel="stdout", since=cursor)


def test_wait_times_out_with_error_fields(local_session):
    """§CORE-06：超时抛 ``LogWaitTimeoutError`` 并携带 pattern/timeout/records/output。"""
    with pytest.raises(LogWaitTimeoutError) as ei:
        local_session.wait("never-appears", timeout=0.15)
    exc = ei.value
    assert exc.pattern == "never-appears"
    assert exc.timeout == 0.15
    assert isinstance(exc.records, tuple)
    assert isinstance(exc.output, str)
    assert exc.command_result is None


def test_wait_raises_when_session_closes_mid_wait(local_session):
    """§CORE-06：等待中会话关闭必须抛 ``SessionClosedError``。"""
    def _closer():
        time.sleep(0.05)
        local_session.close()

    threading.Thread(target=_closer, daemon=True).start()
    with pytest.raises(SessionClosedError):
        local_session.wait("never", timeout=2)


def test_run_wait_no_race(local_session, python_probe):
    """§CORE-06 / 验收标准 11：``run().wait()`` 不会漏掉命令执行期间的匹配。"""
    result = local_session.run(*python_probe(
        "import sys, time; print('BOOT'); sys.stdout.flush(); time.sleep(0.05)"
    ))
    match = result.wait("BOOT", timeout=1)
    assert match.text == "BOOT"
    assert match.command_result is result
    assert match.index == 0


def test_wait_or_list_returns_index(local_session):
    """§CORE-06：list/tuple 为 OR；先命中者胜，``index`` 为序列下标。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="FAILED"
    )
    m = local_session.wait(["READY", "FAILED"], timeout=1, since=cursor)
    assert m.index == 1
    assert m.pattern == "FAILED"
    assert m.text == "FAILED"


def test_wait_empty_sequence_raises(local_session):
    """§CORE-06：空序列抛 ``ValueError``。"""
    with pytest.raises(ValueError):
        local_session.wait([], timeout=0.1)


def test_wait_mixed_text_and_bytes_raises(local_session):
    """§CORE-06：同一调用不得混用文本与字节模式。"""
    with pytest.raises(TypeError):
        local_session.wait(["READY", b"\x06"], timeout=0.1)


def test_wait_multiline_joins_records(local_session):
    """§CORE-06：``multiline=True`` 用换行拼接记录；默认不跨行。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="foo"
    )
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="bar"
    )
    with pytest.raises(LogWaitTimeoutError):
        local_session.wait(re.compile(r"foo\nbar"), timeout=0.15, since=cursor)
    m = local_session.wait(
        re.compile(r"foo\nbar"), timeout=1, since=cursor, multiline=True
    )
    assert m.text == "foo\nbar"
    assert m.record is not None
    assert m.record.message == "bar"


def test_wait_chain_starts_after_match(local_session):
    """§CORE-06：``LogMatch.wait`` 从命中记录之后继续，不回扫更早的模式。"""
    cursor = local_session.logs.cursor()
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="B-early"
    )
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="A"
    )
    first = local_session.wait("A", timeout=1, since=cursor)
    with pytest.raises(LogWaitTimeoutError):
        first.wait("B-early", timeout=0.15)
    local_session.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="B-late"
    )
    second = first.wait("B-late", timeout=1)
    assert second.text == "B-late"


def test_wait_bytes_on_serial(monkeypatch):
    """§CORE-06：串口 ``wait(bytes)`` 匹配原始 RX。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.05)
            port.feed(b"\xaa\x55")

        threading.Thread(target=_feed, daemon=True).start()
        m = sess.wait(b"\xaa\x55", timeout=1)
        assert m.text == b"\xaa\x55"
        assert m.record is None
        assert m.index == 0


def test_wait_bytes_regex(monkeypatch):
    """§CORE-06：``re.Pattern[bytes]`` 在原始 RX 上搜索。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.05)
            port.feed(b"\xaa\x00\xff\x55")

        threading.Thread(target=_feed, daemon=True).start()
        m = sess.wait(re.compile(rb"\xaa.{2}\x55"), timeout=1)
        assert m.text == b"\xaa\x00\xff\x55"


def test_wait_bytes_does_not_split_on_lf(monkeypatch):
    """§CORE-06：字节等待不按 ``\\n`` 分帧。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.05)
            port.feed(b"\x01\n\x02")

        threading.Thread(target=_feed, daemon=True).start()
        m = sess.wait(b"\x01\n\x02", timeout=1)
        assert m.text == b"\x01\n\x02"


def test_run_wait_bytes_no_race(monkeypatch):
    """§CORE-06：``run(bytes).wait(bytes)`` 不漏写出后立即回显的字节。"""
    with open_stub_serial(monkeypatch) as (sess, port):
        port.echo = True
        m = sess.run(b"\x01\x02").wait(b"\x01\x02", timeout=1)
        assert m.text == b"\x01\x02"
        assert m.command_result is not None


def test_wait_bytes_chain_starts_after_match(monkeypatch):
    """§CORE-06：字节链从上一匹配子串结束处继续。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.05)
            port.feed(b"AB")

        threading.Thread(target=_feed, daemon=True).start()
        m = sess.wait(b"A", timeout=1).wait(b"B", timeout=1)
        assert m.text == b"B"


def test_wait_bytes_or_list(monkeypatch):
    """§CORE-06：字节 OR 列表返回命中下标。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.05)
            port.feed(b"\x15")

        threading.Thread(target=_feed, daemon=True).start()
        m = sess.wait([b"\x06", b"\x15"], timeout=1)
        assert m.index == 1
        assert m.pattern == b"\x15"


def test_local_wait_bytes_unsupported(local_session):
    """§CORE-06：非串口对字节模式抛 ``UnsupportedOperationError``。"""
    with pytest.raises(UnsupportedOperationError):
        local_session.wait(b"\x00", timeout=0.1)


def test_wait_bytes_timeout_carries_data(monkeypatch):
    """§CORE-07 / CORE-06：字节等待超时携带 ``data`` 扫描窗口。"""
    with open_stub_serial(monkeypatch) as (sess, port):

        def _feed():
            time.sleep(0.02)
            port.feed(b"xyz")

        threading.Thread(target=_feed, daemon=True).start()
        with pytest.raises(LogWaitTimeoutError) as ei:
            sess.wait(b"\xff", timeout=0.2)
        assert ei.value.data is not None
        assert b"xyz" in ei.value.data


# ----------------------------------------------------- 密码 / env 脱敏


def test_env_values_not_recorded_in_logs(local_session, python_probe):
    """§CORE-06：``env`` 的**值**默认不入日志（避免密码泄漏），只留下 keys。"""
    cursor = local_session.logs.cursor()
    local_session.run(
        *python_probe("import os; print(os.environ.get('SECRET','?'))"),
        env={"SECRET": "s3cr3t-token"},
    )
    recs = local_session.logs.records(since=cursor)
    # command_start 记录中的 fields["env"] 应只含 keys；整个记录文本也不应含值
    starts = [r for r in recs if r.event == "command_start"]
    for r in starts:
        env_field = r.fields.get("env")
        if env_field is not None:
            assert "s3cr3t-token" not in str(env_field)
            assert "SECRET" in str(env_field)
        assert "s3cr3t-token" not in r.message


# ------------------------------------------------------- at() 共享缓冲


def test_at_view_shares_log_buffer(tmp_workspace):
    """§CORE-06：``at()`` 视图与原会话共享同一日志缓冲。"""
    view = tmp_workspace.at(".")
    assert view.logs.buffer is tmp_workspace.logs.buffer


# ------------------------------------------------------- 关闭后仍可读


def test_logs_readable_after_close():
    """§CORE-06 / 验收标准 12：会话关闭后已收集的日志仍可读取。"""
    sess = rpm.local()
    sess.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="LIVES-ON"
    )
    sess.close()
    assert any(
        r.message == "LIVES-ON" for r in sess.logs.records()
    )
