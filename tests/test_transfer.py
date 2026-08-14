"""文件传输 push / pull / copy (CORE-04)。

只测本地-本地场景（含跨 LocalSession 实例），不依赖网络。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import TransferError


def _write(path: Path, content: str = "hi") -> None:
    path.write_text(content, encoding="utf-8")


def test_copy_within_local(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    _write(src, "abc")
    with rpm.local() as sess:
        result = sess.copy(sess.path(str(src)), sess.path(str(dst)))
        assert result.transferred
        assert result.bytes_transferred == 3
        assert dst.read_text() == "abc"


def test_copy_no_overwrite(tmp_path):
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "new")
    _write(dst, "old")
    with rpm.local() as sess:
        r = sess.copy(sess.path(str(src)), sess.path(str(dst)), overwrite=False)
        assert not r.transferred
        assert dst.read_text() == "old"


def test_push_from_another_local_session(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    _write(src, "payload")
    other = rpm.local()
    try:
        with rpm.local() as sess:
            # push: source 属于其他会话，target 属于 sess
            r = sess.push(other.path(str(src)), sess.path(str(dst)))
            assert r.transferred
            assert dst.read_text() == "payload"
    finally:
        other.close()


def test_pull_from_current_to_other_local(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    _write(src, "payload")
    other = rpm.local()
    try:
        with rpm.local() as sess:
            r = sess.pull(sess.path(str(src)), other.path(str(dst)))
            assert r.transferred
            assert dst.read_text() == "payload"
    finally:
        other.close()


def test_push_target_must_belong_to_calling_session(tmp_path):
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src)
    other = rpm.local()
    try:
        with rpm.local() as sess:
            with pytest.raises(TransferError):
                sess.push(sess.path(str(src)), other.path(str(dst)))
    finally:
        other.close()


def test_copy_requires_caller_to_be_involved(tmp_path):
    a = rpm.local()
    b = rpm.local()
    caller = rpm.local()
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src)
    try:
        with pytest.raises(TransferError):
            caller.copy(a.path(str(src)), b.path(str(dst)))
    finally:
        a.close()
        b.close()
        caller.close()


def test_transfer_result_fields(tmp_path):
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    _write(src, "1234567890")
    with rpm.local() as sess:
        r = sess.copy(sess.path(str(src)), sess.path(str(dst)))
        assert r.bytes_transferred == 10
        assert r.duration >= 0
        assert isinstance(r.source, rpm.ResourcePath)
        assert isinstance(r.target, rpm.ResourcePath)
