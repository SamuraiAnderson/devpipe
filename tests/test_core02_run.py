"""CORE-02 命令执行与工作目录（doc/core-lib-requirements.md § CORE-02）。

规格映射：
    §CORE-02/at()/priority          → test_run_cwd_priority_over_at_view
    §CORE-02/at()/immutable         → test_at_cwd_does_not_leak_to_original
    §CORE-02/run/positional         → test_run_positional_captures_stdout
    §CORE-02/run/positional/type    → test_run_positional_args_must_be_str
    §CORE-02/run/shell/single       → test_run_shell_true_executes_pipeline
    §CORE-02/run/shell/no-extra     → test_run_shell_true_forbids_extra_args
    §CORE-02/run/no-naive-join      → test_run_default_does_not_naive_join_args
    §CORE-02/run/check=True         → test_run_check_true_raises_command_error
    §CORE-02/run/check=False        → test_run_check_false_returns_result
    §CORE-02/run/timeout            → test_run_timeout_raises_command_timeout
    §CORE-02/run/timeout/vs-check   → test_timeout_ignores_check_false
    §CORE-02/run/env                → test_run_env_visible_to_subprocess
    §CORE-02/run/encoding           → test_run_encoding_option_decodes_output
    §CORE-02/run/log_command=False  → test_log_command_false_skips_command_start
    §CORE-02/run/log_command=True   → test_log_command_true_records_command_start
    §CORE-02/result/fields          → test_command_result_fields_and_ok
    §CORE-02/result/raise_for_status → test_raise_for_status_matches_check_true
    §CORE-02/result/duration        → test_command_result_duration_positive
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import redpymake as rpm
from redpymake.exceptions import CommandError, CommandTimeoutError


# ------------------------------------------------------------ 位置参数 / shell


def test_run_positional_captures_stdout(local_session, python_probe):
    """§CORE-02：普通模式下每个位置参数独立传递，stdout/stderr 捕获无损。"""
    res = local_session.run(*python_probe("print('hello world')"))
    assert res.returncode == 0
    assert "hello world" in res.stdout
    assert res.ok


def test_run_positional_args_must_be_str(local_session):
    """§CORE-02：``shell=False`` 时非字符串位置参数抛 ``TypeError``。"""
    with pytest.raises(TypeError):
        local_session.run("echo", 123)  # type: ignore[arg-type]


def test_run_shell_true_forbids_extra_args(local_session):
    """§CORE-02：``shell=True`` 只允许一条命令字符串。"""
    with pytest.raises(TypeError):
        local_session.run("echo hi", "extra", shell=True)


def test_run_shell_true_executes_pipeline(local_session, python_bin):
    """§CORE-02：``shell=True`` 允许包含 shell 元字符的完整命令。"""
    cmd = f'"{python_bin}" -c "print(1+2)"'
    res = local_session.run(cmd, shell=True)
    assert "3" in res.stdout


def test_run_default_does_not_naive_join_args(local_session, python_probe):
    """§CORE-02：底层不得对位置参数简单 ``' '.join``；含空格的实参必须完整送达。"""
    argv = python_probe(
        "import sys; sys.stdout.write(repr(sys.argv[1:]))"
    ) + ["hello world", "a b c"]
    res = local_session.run(*argv)
    # 若被 " ".join，则 argv 会退化为 ["hello", "world", "a", "b", "c"] 5 项
    assert "'hello world'" in res.stdout
    assert "'a b c'" in res.stdout


# ------------------------------------------------------------------- check


def test_run_check_true_raises_command_error(local_session, python_probe):
    """§CORE-02：``check=True``（默认）非零退出码抛 ``CommandError`` 且带字段。"""
    with pytest.raises(CommandError) as ei:
        local_session.run(*python_probe("import sys; sys.exit(3)"))
    assert ei.value.returncode == 3
    assert ei.value.command  # 非空


def test_run_check_false_returns_result(local_session, python_probe):
    """§CORE-02：``check=False`` 不抛异常，无论退出码都返回 ``CommandResult``。"""
    res = local_session.run(
        *python_probe("import sys; sys.exit(7)"), check=False
    )
    assert res.returncode == 7
    assert res.ok is False


# ------------------------------------------------------------------ timeout


def test_run_timeout_raises_command_timeout(local_session, python_probe):
    """§CORE-02：超时抛 ``CommandTimeoutError``（继承 ``CommandError``）。"""
    with pytest.raises(CommandTimeoutError) as ei:
        local_session.run(
            *python_probe("import time; time.sleep(3)"), timeout=0.3
        )
    assert ei.value.timeout == 0.3


def test_timeout_ignores_check_false(local_session, python_probe):
    """§CORE-02：连接失败/超时不受 ``check=False`` 抑制，仍抛对应异常。"""
    with pytest.raises(CommandTimeoutError):
        local_session.run(
            *python_probe("import time; time.sleep(3)"),
            timeout=0.3,
            check=False,
        )


# ------------------------------------------------------------ cwd 优先级


def test_run_cwd_priority_over_at_view(tmp_path: Path, python_probe):
    """§CORE-02：``run(cwd=X)`` 覆盖 ``at(Y)``，``at(Y)`` 覆盖会话默认目录。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        view = sess.at("a")
        # 显式 cwd 覆盖 at()
        r1 = view.run(
            *python_probe("import os, sys; sys.stdout.write(os.getcwd())"),
            cwd=str(tmp_path / "b"),
        )
        assert Path(r1.stdout.strip()).resolve() == (tmp_path / "b").resolve()
        # 无显式 cwd，用 at() 目录
        r2 = view.run(
            *python_probe("import os, sys; sys.stdout.write(os.getcwd())"),
        )
        assert Path(r2.stdout.strip()).resolve() == (tmp_path / "a").resolve()


def test_at_cwd_does_not_leak_to_original(tmp_path: Path, python_probe):
    """§CORE-02：``at()`` 派生新视图不改原会话的默认目录。"""
    (tmp_path / "sub").mkdir()
    with rpm.local(default_cwd=str(tmp_path)) as sess:
        sess.at("sub")
        r = sess.run(
            *python_probe("import os, sys; sys.stdout.write(os.getcwd())")
        )
        assert Path(r.stdout.strip()).resolve() == tmp_path.resolve()


# --------------------------------------------------------------------- env


def test_run_env_visible_to_subprocess(local_session, python_probe):
    """§CORE-02：``env`` 中的变量应可被子进程读取。

    实现细节（合并 ``os.environ``）留给实现，但环境变量对子进程必须可见。
    """
    argv = python_probe(
        "import os, sys; sys.stdout.write(os.environ.get('RPM_TEST_VAR','MISS'))"
    )
    res = local_session.run(*argv, env={"RPM_TEST_VAR": "HELLO"})
    assert "HELLO" in res.stdout


# ----------------------------------------------------------------- encoding


def test_run_encoding_option_decodes_output(local_session, python_probe):
    """§CORE-02：``encoding`` 参数决定 stdout/stderr 的解码方式。"""
    argv = python_probe(
        "import sys; sys.stdout.buffer.write(b'caf\\xe9'); sys.stdout.flush()"
    )
    res = local_session.run(*argv, encoding="latin-1")
    assert "café" in res.stdout


# --------------------------------------------------------------- log_command


def test_log_command_false_skips_command_start(local_session, python_probe):
    """§CORE-02：``log_command=False`` 不得写 ``command_start`` 记录。"""
    cursor = local_session.logs.cursor()
    local_session.run(*python_probe("print('X')"), log_command=False)
    events = [
        r.event for r in local_session.logs.records(since=cursor)
    ]
    assert "command_start" not in events
    # 但 stdout 应仍被采集
    assert any(
        r.event == "command_output" and r.message == "X"
        for r in local_session.logs.records(since=cursor)
    )


def test_log_command_true_records_command_start(local_session, python_probe):
    """§CORE-02：``log_command=True``（默认）必须写 ``command_start`` 记录。"""
    cursor = local_session.logs.cursor()
    local_session.run(*python_probe("print('Y')"))
    events = [
        r.event for r in local_session.logs.records(since=cursor)
    ]
    assert "command_start" in events
    assert "command_end" in events


# ---------------------------------------------------------- CommandResult


def test_command_result_fields_and_ok(local_session, python_probe):
    """§CORE-02：``CommandResult`` 暴露 command/returncode/stdout/stderr/duration/session/ok。"""
    res = local_session.run(*python_probe("print('Z')"))
    assert isinstance(res.command, tuple)
    assert res.returncode == 0
    assert "Z" in res.stdout
    assert isinstance(res.stderr, str)
    assert isinstance(res.duration, float)
    assert res.session is local_session
    assert res.ok is True


def test_raise_for_status_matches_check_true(local_session, python_probe):
    """§CORE-02：``result.raise_for_status()`` 与 ``check=True`` 语义等价。"""
    res = local_session.run(
        *python_probe("import sys; sys.exit(5)"), check=False
    )
    assert res.ok is False
    with pytest.raises(CommandError) as ei:
        res.raise_for_status()
    assert ei.value.returncode == 5
    # 成功命令 raise_for_status 不抛
    ok_res = local_session.run(*python_probe("pass"))
    ok_res.raise_for_status()


def test_command_result_duration_positive(local_session, python_probe):
    """§CORE-02：``duration`` 非负且大致覆盖真实耗时。"""
    res = local_session.run(*python_probe("import time; time.sleep(0.05)"))
    assert res.duration >= 0.0
    assert res.duration >= 0.04  # 大致下限（避免测量误差）
