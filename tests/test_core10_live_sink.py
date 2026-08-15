"""CORE-10 实时 NDJSON sink（doc/core-lib-requirements.md § CORE-10）。

规格映射：
    §CORE-10/sink/inactive-by-default    → test_sink_inactive_when_env_missing
    §CORE-10/sink/file-uri-activates     → test_sink_appends_records_when_file_uri
    §CORE-10/sink/meta-lines             → test_sink_emits_begin_end_meta_lines
    §CORE-10/sink/exception-meta         → test_sink_end_meta_carries_exception
    §CORE-10/sink/mkdirs-parent          → test_sink_creates_missing_parent_dir
    §CORE-10/sink/unsubscribe-on-exit    → test_sink_unsubscribes_after_exit
    §CORE-10/sink/schema-fields          → test_sink_line_schema_matches_session_log_record
    §CORE-10/sink/does-not-alter-core09  → test_sink_does_not_alter_core09_behavior
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import redpymake as rpm


def _read_ndjson(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_sink_inactive_when_env_missing(tmp_path: Path, python_probe, monkeypatch):
    """§CORE-10/sink/inactive-by-default：未设置环境变量时不落盘、行为等价 CORE-09。"""
    monkeypatch.delenv("REDPYMAKE_LIVE_SINK", raising=False)
    with rpm.script("nosink") as run:
        with rpm.local() as sess:
            sess.run(*python_probe("print('hi')"))
    # 无环境变量：目录里不该出现任何 NDJSON 文件
    assert list(tmp_path.iterdir()) == []


def test_sink_appends_records_when_file_uri(tmp_path: Path, python_probe, monkeypatch):
    """§CORE-10/sink/file-uri-activates：设置 file:// 后每条记录被追加为一行 JSON。"""
    target = tmp_path / "live.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with rpm.script("hasink") as run:
        with rpm.local() as sess:
            sess.run(*python_probe("print('DATA-LINE')"))
    assert target.exists()
    records = _read_ndjson(target)
    assert any(
        r.get("event") == "command_output" and "DATA-LINE" in r.get("message", "")
        for r in records
    ), f"expected command_output line in {records!r}"


def test_sink_emits_begin_end_meta_lines(tmp_path: Path, monkeypatch):
    """§CORE-10/sink/meta-lines：首尾各有 script.begin / script.end 元行。"""
    target = tmp_path / "meta.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with rpm.script("boundaries"):
        pass
    records = _read_ndjson(target)
    assert records, "expected at least begin+end lines"
    assert records[0].get("event") == "script.begin"
    assert records[0].get("name") == "boundaries"
    assert isinstance(records[0].get("pid"), int)
    assert records[-1].get("event") == "script.end"
    assert records[-1].get("exception") is None


def test_sink_end_meta_carries_exception(tmp_path: Path, monkeypatch):
    """§CORE-10/sink/exception-meta：异常路径下 script.end.exception 非空。"""
    target = tmp_path / "boom.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with pytest.raises(RuntimeError, match="expected"):
        with rpm.script("boom"):
            raise RuntimeError("expected")
    records = _read_ndjson(target)
    end = records[-1]
    assert end.get("event") == "script.end"
    exc = end.get("exception")
    assert isinstance(exc, dict)
    assert exc.get("type") == "RuntimeError"
    assert "expected" in exc.get("message", "")
    assert "Traceback" in exc.get("traceback", "") or exc.get("traceback")


def test_sink_creates_missing_parent_dir(tmp_path: Path, monkeypatch):
    """§CORE-10/sink/mkdirs-parent：父目录不存在时自动创建。"""
    target = tmp_path / "sub" / "deep" / "live.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with rpm.script("mkdirs"):
        pass
    assert target.exists()


def test_sink_unsubscribes_after_exit(tmp_path: Path, python_probe, monkeypatch):
    """§CORE-10/sink/unsubscribe-on-exit：ScriptRun 退出后新事件不再落盘。"""
    target = tmp_path / "unsub.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with rpm.script("unsub"):
        with rpm.local() as sess:
            sess.run(*python_probe("print('INSIDE')"))
    size_after_exit = target.stat().st_size
    with rpm.local() as sess:
        sess.run(*python_probe("print('OUTSIDE')"))
    # 文件应仅由 in-block 事件构成，出块后不再追加
    assert target.stat().st_size == size_after_exit
    content = target.read_text(encoding="utf-8")
    assert "OUTSIDE" not in content


def test_sink_line_schema_matches_session_log_record(tmp_path: Path, python_probe, monkeypatch):
    """§CORE-10/sink/schema-fields：每行都含 SessionLogRecord 关键字段。"""
    target = tmp_path / "schema.ndjson"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{target.as_posix()}")
    with rpm.script("schema"):
        with rpm.local() as sess:
            sess.run(*python_probe("print('S')"))
    records = _read_ndjson(target)
    # 找一条命令输出行
    cmd_line = next(r for r in records if r.get("event") == "command_output")
    for key in ("timestamp", "sequence", "session_id", "event", "level", "stream", "message"):
        assert key in cmd_line, f"missing key {key!r} in {cmd_line!r}"


def test_sink_does_not_alter_core09_behavior(tmp_path: Path, python_probe, monkeypatch):
    """§CORE-10/sink/does-not-alter-core09：开启 sink 时 CORE-09 的 snapshot 与 dump_on_error 仍生效。"""
    sink_path = tmp_path / "live.ndjson"
    dump_path = tmp_path / "dump.log"
    monkeypatch.setenv("REDPYMAKE_LIVE_SINK", f"file://{sink_path.as_posix()}")
    with pytest.raises(RuntimeError, match="both"):
        with rpm.script("both", dump_on_error=str(dump_path)) as run:
            with rpm.local() as sess:
                sess.run(*python_probe("print('BOTH')"))
            raise RuntimeError("both")
    # 两个落盘目标都非空
    assert sink_path.exists() and sink_path.stat().st_size > 0
    assert dump_path.exists()
    dump_text = dump_path.read_text(encoding="utf-8")
    assert "BOTH" in dump_text
    assert "RuntimeError" in dump_text
