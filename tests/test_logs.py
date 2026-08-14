"""日志缓冲、tag、订阅、保存 (CORE-06)。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import redpymake as rpm


def test_logs_records_and_text():
    with rpm.local() as sess:
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="line-1"
        )
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="line-2"
        )
        records = sess.logs.records()
        outs = [r for r in records if r.event == "command_output"]
        assert [r.message for r in outs] == ["line-1", "line-2"]
        assert sess.logs.text() == "line-1\nline-2"


def test_logs_subscribe_and_unsubscribe():
    with rpm.local() as sess:
        received = []
        unsubscribe = sess.logs.subscribe(lambda r: received.append(r.message))
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="hi"
        )
        assert received == ["hi"]
        unsubscribe()
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="ignored"
        )
        assert received == ["hi"]


def test_logs_save(tmp_path):
    with rpm.local() as sess:
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="alpha"
        )
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="beta"
        )
        out = tmp_path / "sub" / "log.txt"
        sess.logs.save(str(out))
        assert out.read_text().splitlines() == ["alpha", "beta"]


def test_logs_tag_attaches_fields():
    with rpm.local() as sess:
        with sess.logs.tag(step="build", branch="main"):
            sess.logs.buffer.append(
                event="command_output",
                level="INFO",
                stream="stdout",
                message="hi",
            )
        outside = None
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="bye"
        )
        records = sess.logs.records()
        outs = [r for r in records if r.event == "command_output"]
        tagged = next(r for r in outs if r.message == "hi")
        assert tagged.fields.get("step") == "build"
        assert tagged.fields.get("branch") == "main"
        untagged = next(r for r in outs if r.message == "bye")
        assert "step" not in untagged.fields


def test_logs_tag_nested_inner_wins():
    with rpm.local() as sess:
        with sess.logs.tag(step="outer"):
            with sess.logs.tag(step="inner"):
                sess.logs.buffer.append(
                    event="command_output",
                    level="INFO",
                    stream="stdout",
                    message="x",
                )
        rec = [
            r for r in sess.logs.records() if r.event == "command_output"
        ][-1]
        assert rec.fields.get("step") == "inner"


def test_logs_readable_after_close():
    sess = rpm.local()
    sess.logs.buffer.append(
        event="command_output", level="INFO", stream="stdout", message="hi"
    )
    sess.close()
    assert any(r.message == "hi" for r in sess.logs.records())


def test_cursor_reflects_next_sequence():
    with rpm.local() as sess:
        cursor = sess.logs.cursor()
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="one"
        )
        recs = sess.logs.records(since=cursor)
        assert any(r.message == "one" for r in recs)


def test_capacity_ring():
    from redpymake._logs import LogBuffer

    buf = LogBuffer("t", capacity=3)
    for i in range(5):
        buf.append(
            event="command_output",
            level="INFO",
            stream="stdout",
            message=str(i),
        )
    # 只保留最近 3 条
    msgs = [r.message for r in buf.records()]
    assert msgs == ["2", "3", "4"]
    # 但 sequence 保持递增
    seqs = [r.sequence for r in buf.records()]
    assert seqs == sorted(seqs)
