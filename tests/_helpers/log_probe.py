"""日志断言便捷函数。

在契约测试中，我们经常需要"从会话日志里找出与某命令关联的所有记录"或"断言某
事件至少出现一次"。这些谓词放在这里避免各测试文件重复。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from redpymake._logs import SessionLogRecord


def find_events(records: Iterable[SessionLogRecord], event: str) -> list[SessionLogRecord]:
    """筛选出所有 ``event`` 匹配的记录。"""
    return [r for r in records if r.event == event]


def find_by_op(
    records: Iterable[SessionLogRecord], operation_id: str
) -> list[SessionLogRecord]:
    """筛选出所有属于同一 ``operation_id`` 的记录。"""
    return [r for r in records if r.operation_id == operation_id]


def expect_event(
    records: Sequence[SessionLogRecord], event: str, *, hint: str = ""
) -> list[SessionLogRecord]:
    """断言至少存在一条 ``event`` 匹配的记录，返回全部匹配。"""
    matched = find_events(records, event)
    assert matched, (
        f"expected event {event!r} to appear in records"
        + (f" ({hint})" if hint else "")
        + f"; got events: {[r.event for r in records]}"
    )
    return matched


def expect_field(
    record: SessionLogRecord, key: str, value=..., *, hint: str = ""
):
    """断言 ``record.fields[key]`` 存在（value 为默认值时）或等于 value。"""
    assert key in record.fields, (
        f"expected field {key!r} in record {record!r}"
        + (f" ({hint})" if hint else "")
    )
    if value is not ...:
        assert record.fields[key] == value, (
            f"expected fields[{key!r}] == {value!r}, got {record.fields[key]!r}"
            + (f" ({hint})" if hint else "")
        )
    return record.fields[key]


__all__ = ["find_events", "find_by_op", "expect_event", "expect_field"]
