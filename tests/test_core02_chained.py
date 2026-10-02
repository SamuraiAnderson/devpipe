"""Specification mapping: §CORE-02/chain → command routing, capture and errors."""
import re
import pytest
import redpymake as rpm
from ._helpers.chain_host import install_host, Reply


def test_chain_command_logs_belong_to_device(monkeypatch):
    """§CORE-02/chain：输出/游标属于终点，宿主不重复记设备命令。"""
    install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        with host.adb("SN") as dev:
            result = dev.run("echo", "hello")
            assert result.ok and result.wait("hello").text == "hello"
            assert "hello" in dev.logs.text()
            assert "hello" not in host.logs.text()


def test_ssh_reads_all_tail_bytes_before_close(monkeypatch):
    """§CORE-02/chain：退出码就绪也必须读完 stdout/stderr。"""
    wire = install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        wire.handler = lambda command: Reply("你好\n".encode() * 4096 + b"tail", b"error-tail", chunk=1)
        result = host.run("large-output")
        assert result.stdout == "你好\n" * 4096 + "tail"
        assert result.stderr == "error-tail"


def test_ssh_reader_failure_is_not_eof(monkeypatch):
    """§CORE-02/chain：读取异常传播，check=False 也不能伪装成功。"""
    wire = install_host(monkeypatch)
    with rpm.ssh("lab") as host:
        wire.handler = lambda command: Reply(read_error=OSError("wire broke"))
        with pytest.raises(rpm.SessionConnectionError, match="wire broke"):
            host.run("read", check=False)


def test_adb_missing_completion_marker_is_not_success(monkeypatch):
    """§CORE-02/chain：未确认终点退出状态时不能以 SSH 成功代替。"""
    wire = install_host(monkeypatch)
    with rpm.ssh("lab").adb("SN") as dev:
        wire.handler = lambda command: Reply(b"partial")
        with pytest.raises(rpm.SessionConnectionError):
            dev.run("reboot", check=False)


def test_adb_check_and_literal_arguments(monkeypatch):
    """§CORE-02/chain：逐层保留字面量参数和终点退出码。"""
    import shlex
    wire = install_host(monkeypatch)
    def respond(command):
        if "__RPM_EXIT_" in command:
            inner = shlex.split(command)[-1]
            assert "'a b'" in inner and "'$HOME'" in inner and "'*.txt'" in inner
            marker = re.search(r"__RPM_EXIT_[\w-]+__", inner).group()
            return Reply((marker + "7\n").encode(), b"device-error")
    with rpm.ssh("lab").adb("SN") as dev:
        wire.handler = respond
        assert dev.run("cmd", "a b", "$HOME", "*.txt", check=False).rc == 7
        with pytest.raises(rpm.CommandError) as failure:
            dev.run("cmd", "a b", "$HOME", "*.txt")
        assert failure.value.stderr == "device-error"


@pytest.mark.parametrize("kind,platform", [("adb", "posix"), ("wsl", "windows")])
def test_chained_shell_env_is_set_before_expansion(monkeypatch, kind, platform):
    """§CORE-02/chain：shell=True 的环境在终点 shell 展开变量前设置。"""
    wire = install_host(monkeypatch, platform)
    with getattr(rpm.ssh("host"), kind)("device") as device:
        def check(command):
            assert "export RPM_VALUE=" in command
            assert command.index("export RPM_VALUE=") < command.index("echo $RPM_VALUE")
        wire.handler = check
        device.at("/endpoint").run("echo $RPM_VALUE", shell=True, env={"RPM_VALUE": "a b"})
