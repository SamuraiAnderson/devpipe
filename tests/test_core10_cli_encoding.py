"""Specification mapping: §CORE-10/cli/encoding → terminal-safe text and errors."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["discover", "targets", "run", "exec"])
def test_cli_gbk_streams_preserve_chinese_and_escape_unrepresentable(tmp_path, command):
    """§CORE-10/cli/encoding：GBK 输出保留中文，用转义表示不可编码字符。"""
    (tmp_path / "rpm_demo.py").write_text(
        'import redpymake as rpm\nwith rpm.script("中文🚀"):\n    pass\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.redpymake.targets.box]\nkind = "local"\ndefault_cwd = "中文🚀"\n', encoding="utf-8"
    )
    if command == "discover":
        argv = [command, str(tmp_path)]
    elif command == "targets":
        argv = [command, "--root", str(tmp_path)]
    elif command == "run":
        argv = [command, str(tmp_path / "中文🚀.py")]
    else:
        # Override the deliberately non-existent configured cwd for this command.
        code = "import sys; sys.stdout.buffer.write('中文🚀'.encode('utf-8')); sys.stderr.buffer.write('失败🚀'.encode('utf-8')); sys.exit(7)"
        argv = [command, "--root", str(tmp_path), "--cwd", str(tmp_path), "box", sys.executable, "-c", code]
    result = subprocess.run(
        [sys.executable, "-m", "redpymake", *argv], capture_output=True, timeout=10,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )
    expected_rc = {"discover": 0, "targets": 0, "run": 66, "exec": 7}[command]
    assert result.returncode == expected_rc, result.stderr.decode("gbk", errors="replace")
    text = (result.stdout + result.stderr).decode("gbk")
    assert "中文" in text
    assert "\\U0001f680" in text
    assert "Traceback" not in text


@pytest.mark.parametrize("command", ["discover", "targets"])
def test_cli_gbk_json_round_trips_unicode(tmp_path, command):
    """§CORE-10/cli/encoding：JSON 转义保持合法且解析后还原完整 Unicode。"""
    (tmp_path / "rpm_demo.py").write_text(
        'import redpymake as rpm\nwith rpm.script("中文🚀"):\n    pass\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.redpymake.targets.box]\nkind = "local"\ndefault_cwd = "中文🚀"\n', encoding="utf-8"
    )
    argv = [command, str(tmp_path)] if command == "discover" else [command, "--root", str(tmp_path)]
    result = subprocess.run(
        [sys.executable, "-m", "redpymake", *argv, "--json"], capture_output=True, timeout=10,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )
    assert result.returncode == 0, result.stderr.decode("gbk")
    payload = json.loads(result.stdout.decode("gbk"))
    value = payload[0]["script_name"] if command == "discover" else payload[0]["params"]["default_cwd"]
    assert value == "中文🚀"


def test_cli_gbk_argparse_error_keeps_usage_exit_code():
    """§CORE-10/cli/encoding：含不可编码参数的用法错误仍退出 64。"""
    result = subprocess.run(
        [sys.executable, "-m", "redpymake", "中文🚀"], capture_output=True, timeout=10,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )
    assert result.returncode == 64
    assert "中文\\U0001f680" in result.stderr.decode("gbk")
    assert b"Traceback" not in result.stderr
