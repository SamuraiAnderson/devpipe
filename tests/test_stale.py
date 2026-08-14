"""``rpm.stale`` 顶级函数与 mtime 策略 (CORE-05)。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import InputNotFoundError, UnsupportedOperationError


def _touch(p: Path, content: str = "x") -> None:
    p.write_text(content)


def _set_mtime(p: Path, mtime: float) -> None:
    os.utime(p, (mtime, mtime))


def test_stale_target_missing(tmp_path):
    src = tmp_path / "src.txt"
    tgt = tmp_path / "tgt.bin"
    _touch(src)
    with rpm.local() as sess:
        assert rpm.stale(sess.path(str(tgt)), depends_on=sess.path(str(src)))


def test_stale_source_newer(tmp_path):
    src = tmp_path / "src.txt"
    tgt = tmp_path / "tgt.bin"
    _touch(tgt)
    _touch(src)
    _set_mtime(tgt, time.time() - 10)
    _set_mtime(src, time.time())
    with rpm.local() as sess:
        assert rpm.stale(sess.path(str(tgt)), depends_on=sess.path(str(src)))


def test_stale_up_to_date(tmp_path):
    src = tmp_path / "src.txt"
    tgt = tmp_path / "tgt.bin"
    _touch(src)
    _touch(tgt)
    _set_mtime(src, time.time() - 100)
    _set_mtime(tgt, time.time())
    with rpm.local() as sess:
        assert rpm.stale(sess.path(str(tgt)), depends_on=sess.path(str(src))) is False


def test_stale_input_not_found_raises(tmp_path):
    tgt = tmp_path / "t.bin"
    with rpm.local() as sess:
        with pytest.raises(InputNotFoundError) as ei:
            rpm.stale(
                sess.path(str(tgt)),
                depends_on=sess.path(str(tmp_path / "missing.src")),
                name="deploy",
            )
        assert ei.value.name == "deploy"


def test_stale_accepts_str_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "s.txt")
    # target 缺失 → True
    assert rpm.stale("t.bin", depends_on="s.txt") is True


def test_stale_multiple_targets_and_sources(tmp_path):
    with rpm.local() as sess:
        a = tmp_path / "a"; _touch(a)
        b = tmp_path / "b"; _touch(b)
        t1 = tmp_path / "t1"; _touch(t1)
        t2 = tmp_path / "t2"; _touch(t2)
        # sources newer than targets
        _set_mtime(t1, time.time() - 100)
        _set_mtime(t2, time.time() - 100)
        _set_mtime(a, time.time())
        _set_mtime(b, time.time() - 50)
        assert rpm.stale(
            [sess.path(str(t1)), sess.path(str(t2))],
            depends_on=[sess.path(str(a)), sess.path(str(b))],
        )


def test_stale_hash_strategy_unsupported(tmp_path):
    with rpm.local() as sess:
        with pytest.raises(UnsupportedOperationError):
            rpm.stale(
                sess.path(str(tmp_path / "t")),
                depends_on=sess.path(str(tmp_path / "s")),
                strategy="hash",
            )


def test_stale_custom_predicate(tmp_path):
    called = {}
    def predicate(targets, sources):
        called["ok"] = True
        return True

    with rpm.local() as sess:
        assert rpm.stale(
            sess.path(str(tmp_path / "t")),
            depends_on=(),
            strategy=predicate,
        )
    assert called["ok"]


def test_stale_writes_log_record(tmp_path):
    src = tmp_path / "s"; _touch(src)
    tgt = tmp_path / "t"
    with rpm.local() as sess:
        assert rpm.stale(sess.path(str(tgt)), depends_on=sess.path(str(src)), name="deploy")
        records = [r for r in sess.logs.records() if r.event == "stale.check"]
        assert records, "expected stale.check log record"
        r = records[-1]
        assert r.fields.get("name") == "deploy"
        assert r.fields.get("result") is True
        assert r.fields.get("reason") == "target_missing"


def test_stale_no_sources_returns_false_when_target_exists(tmp_path):
    tgt = tmp_path / "t"; _touch(tgt)
    with rpm.local() as sess:
        assert rpm.stale(sess.path(str(tgt)), depends_on=()) is False
