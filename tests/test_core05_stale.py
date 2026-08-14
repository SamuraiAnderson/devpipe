"""CORE-05 过时判断（doc/core-lib-requirements.md § CORE-05）。

规格映射：
    §CORE-05/rule/target_missing        → test_mtime_rules[target_missing]
    §CORE-05/rule/source_newer          → test_mtime_rules[source_newer]
    §CORE-05/rule/up_to_date            → test_mtime_rules[up_to_date]
    §CORE-05/input-missing-always-raise → test_input_not_found_always_raises
    §CORE-05/input-missing-carries-name → test_input_not_found_carries_name
    §CORE-05/pathspec/str               → test_stale_accepts_str_paths
    §CORE-05/pathspec/pathlib           → test_stale_accepts_pathlib_paths
    §CORE-05/pathspec/multiple          → test_stale_multiple_targets_and_sources
    §CORE-05/no-sources/existing-target → test_no_sources_returns_false_when_target_exists
    §CORE-05/strategy/hash-reserved     → test_hash_strategy_unsupported
    §CORE-05/strategy/custom            → test_custom_predicate_invoked
    §CORE-05/strategy/unknown           → test_unknown_strategy_raises
    §CORE-05/log/stale.check            → test_stale_writes_log_record
    §CORE-05/log/reason-source-newer    → test_source_newer_reason_in_log
    §CORE-05/returns-bool               → test_stale_returns_plain_bool
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import (
    InputNotFoundError,
    UnsupportedOperationError,
)


def _touch(p: Path, content: str = "x") -> None:
    p.write_text(content)


def _set_mtime(p: Path, mtime: float) -> None:
    os.utime(p, (mtime, mtime))


# --------------------------------------------------------- 三分支规则表


@pytest.mark.parametrize(
    "case,expected,reason",
    [
        pytest.param("target_missing", True, "target_missing", id="target_missing"),
        pytest.param("source_newer", True, "source_newer", id="source_newer"),
        pytest.param("up_to_date", False, "up_to_date", id="up_to_date"),
    ],
)
def test_mtime_rules(local_session, tmp_path: Path, case, expected, reason):
    """§CORE-05：mtime 策略三分支的判断规则表。"""
    src = tmp_path / "src.txt"
    tgt = tmp_path / "tgt.bin"
    _touch(src)
    now = time.time()

    if case == "target_missing":
        # 目标不存在（无需 target 文件）
        pass
    elif case == "source_newer":
        _touch(tgt)
        _set_mtime(tgt, now - 100)
        _set_mtime(src, now)
    elif case == "up_to_date":
        _touch(tgt)
        _set_mtime(src, now - 100)
        _set_mtime(tgt, now)

    result = rpm.stale(
        local_session.path(str(tgt)),
        depends_on=local_session.path(str(src)),
        name="probe",
    )
    assert result is expected

    # 日志里的 reason 应与规则表一致
    recs = [r for r in local_session.logs.records() if r.event == "stale.check"]
    assert recs[-1].fields.get("reason") == reason


# ----------------------------------------------------------- 依赖不存在


def test_input_not_found_always_raises(local_session, tmp_path: Path):
    """§CORE-05：依赖不存在始终抛 ``InputNotFoundError``，不当作 True。"""
    with pytest.raises(InputNotFoundError):
        rpm.stale(
            local_session.path(str(tmp_path / "t")),
            depends_on=local_session.path(str(tmp_path / "missing.src")),
        )


def test_input_not_found_carries_name(local_session, tmp_path: Path):
    """§CORE-05：异常字段 ``name`` 携带 stale 的可读标识。"""
    with pytest.raises(InputNotFoundError) as ei:
        rpm.stale(
            local_session.path(str(tmp_path / "t")),
            depends_on=local_session.path(str(tmp_path / "missing.src")),
            name="deploy_app",
        )
    assert ei.value.name == "deploy_app"


# ------------------------------------------------------- PathSpec 支持


def test_stale_accepts_str_paths(tmp_path: Path, monkeypatch):
    """§CORE-05：``str`` 相对当前进程目录解析。"""
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "s.txt")
    assert rpm.stale("t.bin", depends_on="s.txt") is True


def test_stale_accepts_pathlib_paths(tmp_path: Path):
    """§CORE-05：``pathlib.Path`` 也可作为 ``PathSpec``。"""
    src = tmp_path / "s2.txt"
    tgt = tmp_path / "t2.bin"
    _touch(src)
    _touch(tgt)
    _set_mtime(src, time.time() - 100)
    _set_mtime(tgt, time.time())
    assert rpm.stale(tgt, depends_on=src) is False


def test_stale_multiple_targets_and_sources(local_session, tmp_path: Path):
    """§CORE-05：支持多个目标与多个依赖。"""
    a = tmp_path / "a"; _touch(a)
    b = tmp_path / "b"; _touch(b)
    t1 = tmp_path / "t1"; _touch(t1)
    t2 = tmp_path / "t2"; _touch(t2)
    now = time.time()
    _set_mtime(t1, now - 100)
    _set_mtime(t2, now - 100)
    _set_mtime(a, now)
    _set_mtime(b, now - 50)
    assert rpm.stale(
        [local_session.path(str(t1)), local_session.path(str(t2))],
        depends_on=[local_session.path(str(a)), local_session.path(str(b))],
    ) is True


def test_no_sources_returns_false_when_target_exists(local_session, tmp_path: Path):
    """§CORE-05：无依赖 + 目标存在 → 视为不过时。"""
    tgt = tmp_path / "t"; _touch(tgt)
    assert rpm.stale(
        local_session.path(str(tgt)), depends_on=()
    ) is False


# --------------------------------------------------------------- strategy


def test_hash_strategy_unsupported(local_session, tmp_path: Path):
    """§CORE-05：``strategy='hash'`` 预留，第一版必须抛 ``UnsupportedOperationError``。"""
    with pytest.raises(UnsupportedOperationError):
        rpm.stale(
            local_session.path(str(tmp_path / "t")),
            depends_on=local_session.path(str(tmp_path / "s")),
            strategy="hash",
        )


def test_custom_predicate_invoked(local_session, tmp_path: Path):
    """§CORE-05：自定义 ``StalePredicate`` 会被调用并决定返回值。"""
    calls: list[tuple[int, int]] = []

    def predicate(targets, sources):
        calls.append((len(targets), len(sources)))
        return True

    assert rpm.stale(
        local_session.path(str(tmp_path / "t")),
        depends_on=(),
        strategy=predicate,
    ) is True
    assert calls == [(1, 0)]


def test_unknown_strategy_raises(local_session, tmp_path: Path):
    """§CORE-05：未知字符串策略应抛 ``ValueError``。"""
    with pytest.raises(ValueError):
        rpm.stale(
            local_session.path(str(tmp_path / "t")),
            depends_on=(),
            strategy="not-a-strategy",
        )


# ------------------------------------------------------------------ 日志


def test_stale_writes_log_record(local_session, tmp_path: Path):
    """§CORE-05：每次 stale 求值应写一条 ``stale.check`` 日志，字段完整。"""
    src = tmp_path / "s"; _touch(src)
    tgt = tmp_path / "t"
    assert rpm.stale(
        local_session.path(str(tgt)),
        depends_on=local_session.path(str(src)),
        name="deploy",
    ) is True
    recs = [r for r in local_session.logs.records() if r.event == "stale.check"]
    assert recs, "expected at least one stale.check record"
    r = recs[-1]
    assert r.fields.get("name") == "deploy"
    assert r.fields.get("result") is True
    assert r.fields.get("reason") == "target_missing"
    assert r.fields.get("strategy") == "mtime"
    assert isinstance(r.fields.get("targets"), list)
    assert isinstance(r.fields.get("depends_on"), list)
    assert "elapsed" in r.fields


def test_source_newer_reason_in_log(local_session, tmp_path: Path):
    """§CORE-05：``source_newer`` 分支日志中 ``reason`` 为 ``source_newer``。"""
    src = tmp_path / "s"; _touch(src)
    tgt = tmp_path / "t"; _touch(tgt)
    _set_mtime(tgt, time.time() - 100)
    _set_mtime(src, time.time())
    rpm.stale(
        local_session.path(str(tgt)),
        depends_on=local_session.path(str(src)),
    )
    r = [r for r in local_session.logs.records() if r.event == "stale.check"][-1]
    assert r.fields.get("reason") == "source_newer"


def test_stale_returns_plain_bool(local_session, tmp_path: Path):
    """§CORE-05：``rpm.stale`` 只返回 ``bool``，不返回枚举/元组。"""
    src = tmp_path / "s"; _touch(src)
    tgt = tmp_path / "t"; _touch(tgt)
    _set_mtime(src, time.time() - 100)
    _set_mtime(tgt, time.time())
    result = rpm.stale(
        local_session.path(str(tgt)),
        depends_on=local_session.path(str(src)),
    )
    assert isinstance(result, bool)
    assert result is False
