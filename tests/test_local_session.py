"""LocalSession + CommandResult + at()/path()/wait()/run().wait() (CORE-01/02/03/06)。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time

import pytest

import redpymake as rpm
from redpymake.exceptions import (
    CommandError,
    CommandTimeoutError,
    LogWaitTimeoutError,
    SessionClosedError,
)


# --------------------------- 基础生命周期 ---------------------------


def test_local_factory_returns_session():
    with rpm.local() as sess:
        assert isinstance(sess, rpm.LocalSession)
        assert sess.kind == "local"
        assert not sess.closed
    assert sess.closed


def test_close_idempotent():
    sess = rpm.local()
    sess.close()
    sess.close()  # 不应抛异常


def test_closed_session_rejects_ops(python_bin):
    sess = rpm.local()
    sess.close()
    with pytest.raises(SessionClosedError):
        sess.run(python_bin, "-c", "pass")
    # 读取历史日志仍可用
    assert isinstance(sess.logs.records(), list)


# --------------------------- run() 语义 ---------------------------


def test_run_captures_stdout(python_bin):
    with rpm.local() as sess:
        res = sess.run(python_bin, "-c", "print('hello')")
        assert res.returncode == 0
        assert res.ok
        assert "hello" in res.stdout
        assert res.session is sess


def test_run_stderr_separate(python_bin):
    with rpm.local() as sess:
        res = sess.run(
            python_bin,
            "-c",
            "import sys; sys.stderr.write('err\\n')",
        )
        assert res.returncode == 0
        assert "err" in res.stderr
        assert "err" not in res.stdout


def test_run_check_true_raises(python_bin):
    with rpm.local() as sess:
        with pytest.raises(CommandError) as ei:
            sess.run(python_bin, "-c", "import sys; sys.exit(3)")
        assert ei.value.returncode == 3
        assert ei.value.command[0] == python_bin


def test_run_check_false_returns_result(python_bin):
    with rpm.local() as sess:
        res = sess.run(python_bin, "-c", "import sys; sys.exit(7)", check=False)
        assert res.returncode == 7
        assert not res.ok


def test_run_shell_true_forbids_extra_args():
    with rpm.local() as sess:
        with pytest.raises(TypeError):
            sess.run("echo hi", "extra", shell=True)


def test_run_shell_true_executes(python_bin):
    with rpm.local() as sess:
        cmd = f'"{python_bin}" -c "print(1+2)"'
        res = sess.run(cmd, shell=True)
        assert "3" in res.stdout


def test_run_timeout_raises(python_bin):
    with rpm.local() as sess:
        with pytest.raises(CommandTimeoutError):
            sess.run(
                python_bin,
                "-c",
                "import time; time.sleep(2)",
                timeout=0.3,
            )


def test_run_positional_args_type_check():
    with rpm.local() as sess:
        with pytest.raises(TypeError):
            sess.run("echo", 123)  # type: ignore[arg-type]


# --------------------------- at() 视图 ---------------------------


def test_at_view_shares_log_buffer():
    with rpm.local() as sess:
        view = sess.at(".")
        assert view.logs.buffer is sess.logs.buffer
        assert view.root is sess


def test_at_view_does_not_mutate_original(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view = sess.at("sub")
        assert os.path.normpath(view.default_cwd) == os.path.normpath(str(sub))
        assert os.path.normpath(sess.default_cwd) == os.path.normpath(str(tmp_path))


def test_run_cwd_priority(tmp_path, python_bin):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view = sess.at("a")
        # 显式 cwd=... 覆盖 at() 目录
        res = view.run(
            python_bin,
            "-c",
            "import os, sys; sys.stdout.write(os.getcwd())",
            cwd=str(tmp_path / "b"),
        )
        assert os.path.normpath(res.stdout.strip()).lower() == os.path.normpath(str(tmp_path / "b")).lower()
        # 无显式 cwd 时用 at() 目录
        res2 = view.run(
            python_bin,
            "-c",
            "import os, sys; sys.stdout.write(os.getcwd())",
        )
        assert os.path.normpath(res2.stdout.strip()).lower() == os.path.normpath(str(tmp_path / "a")).lower()


def test_at_chained(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        deep = sess.at("a").at("b")
        assert os.path.normpath(deep.default_cwd) == os.path.normpath(str(tmp_path / "a" / "b"))


# --------------------------- wait() 语义 ---------------------------


def test_wait_matches_new_record_only_by_default(python_bin):
    with rpm.local() as sess:
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="OLD"
        )
        # 默认 since=None → 从当前时刻向后
        with pytest.raises(LogWaitTimeoutError):
            sess.wait("OLD", timeout=0.15)


def test_wait_matches_with_explicit_cursor():
    with rpm.local() as sess:
        cursor = sess.logs.cursor()
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stdout", message="HELLO"
        )
        m = sess.wait("HELLO", timeout=1, since=cursor)
        assert m.text == "HELLO"


def test_wait_regex_pattern():
    with rpm.local() as sess:
        cursor = sess.logs.cursor()
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="serial", message="login: "
        )
        m = sess.wait(re.compile(r"login:\s*$"), timeout=1, since=cursor)
        assert "login:" in m.text


def test_wait_channel_filter():
    with rpm.local() as sess:
        cursor = sess.logs.cursor()
        sess.logs.buffer.append(
            event="command_output", level="INFO", stream="stderr", message="ERR-XYZ"
        )
        with pytest.raises(LogWaitTimeoutError):
            sess.wait("ERR-XYZ", timeout=0.15, channel="stdout", since=cursor)


def test_wait_times_out():
    with rpm.local() as sess:
        with pytest.raises(LogWaitTimeoutError):
            sess.wait("never-appears", timeout=0.2)


def test_wait_wakes_up_on_async_append():
    with rpm.local() as sess:
        def _later():
            time.sleep(0.05)
            sess.logs.buffer.append(
                event="command_output",
                level="INFO",
                stream="stdout",
                message="LATE",
            )

        threading.Thread(target=_later, daemon=True).start()
        m = sess.wait("LATE", timeout=2)
        assert m.text == "LATE"


def test_run_wait_no_race(python_bin):
    """``run().wait()`` 必须能匹配到命令执行期间已产生的输出。"""
    with rpm.local() as sess:
        script = (
            "import sys, time;"
            "print('BOOT');"
            "sys.stdout.flush();"
            "time.sleep(0.05)"
        )
        result = sess.run(python_bin, "-c", script)
        match = result.wait("BOOT", timeout=1)
        assert match.text == "BOOT"
        assert match.command_result is result
