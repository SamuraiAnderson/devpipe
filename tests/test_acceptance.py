"""第 5 节 验收标准（doc/core-lib-requirements.md § 5. 验收标准）。

每条验收标准对应一个测试；这里覆盖那些**能在本地默认 CI 环境跑通**的项目。
需要真实 SSH/ADB/串口的第 8 条见 ``tests/integration/`` 骨架。

规格映射：
    §5/1  README 前十分钟示例                       → test_acceptance_1_readme_first_10min
    §5/2  无需 sys.path.insert                      → test_acceptance_2_no_sys_path_hack_needed
    §5/3  核心操作无需判断会话类型                  → test_acceptance_3_no_isinstance_branching
    §5/4  所有 run() 返回同类型 CommandResult       → test_acceptance_4_all_run_returns_command_result
    §5/5  传输通过 push/pull/copy 无平台分支        → test_acceptance_5_transfer_no_platform_branch
    §5/6  过时判断使用顶级函数 rpm.stale() → bool   → test_acceptance_6_stale_is_plain_function
    §5/7  默认单测无需网络/真实设备                 → test_acceptance_7_no_integration_default
    §5/8  SSH/ADB/串口标记为集成测试                → test_acceptance_8_integration_scaffold_present
    §5/9  覆盖率建议 ≥ 80%                          → 由 CI pytest --cov 生成
    §5/10 UI 移除后核心库仍可独立运行               → test_acceptance_10_core_installable_alone
    §5/11 run().wait() 无竞态                       → test_acceptance_11_run_wait_no_race
    §5/12 关闭后仍可读日志                          → test_acceptance_12_logs_readable_after_close
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import redpymake as rpm

from ._helpers.fake_remote import FakeRemoteSession


# 5/1 -----------------------------------------------------------------


def test_acceptance_1_readme_first_10min(local_session, python_probe, tmp_path):
    """§5/1：README 前十分钟示例应完成本地命令 + 文件复制。"""
    src = tmp_path / "hello.txt"
    dst = tmp_path / "hello.copy.txt"
    src.write_text("hi")
    r = local_session.run(*python_probe("print(2+3)"))
    assert "5" in r.stdout
    tr = local_session.copy(
        local_session.path(str(src)), local_session.path(str(dst))
    )
    assert tr.transferred is True
    assert dst.read_text() == "hi"


# 5/2 -----------------------------------------------------------------


def test_acceptance_2_no_sys_path_hack_needed():
    """§5/2：使用者不需要 ``sys.path.insert``；``import redpymake`` 直接可用。"""
    # 在子进程中启动一个空 sys.path 的解释器：由 pip install -e . 或 site-packages
    # 保证 redpymake 可导入。若这一测试跑绿，说明用户不需要 sys.path hack。
    code = "import redpymake as rpm; print(rpm.__version__)"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()


# 5/3 -----------------------------------------------------------------


def test_acceptance_3_no_isinstance_branching(
    local_session, fake_remote: FakeRemoteSession, python_probe, tmp_path
):
    """§5/3：核心操作对所有会话统一调用，不需要判断具体类型。

    以传输为例：无论 caller 是 local 还是 fake-remote，都用同一批 API。
    """
    src = tmp_path / "a"
    src.write_text("common")
    # 同一 API，两种 caller 都走通
    r1 = local_session.copy(
        local_session.path(str(src)),
        local_session.path(str(tmp_path / "b")),
    )
    r2 = fake_remote.push(
        local_session.path(str(src)), fake_remote.path("/x")
    )
    assert r1.transferred and r2.transferred


# 5/4 -----------------------------------------------------------------


def test_acceptance_4_all_run_returns_command_result(local_session, python_probe):
    """§5/4：``run()`` 一律返回 ``CommandResult`` 类型。"""
    r = local_session.run(*python_probe("print(1)"))
    assert isinstance(r, rpm.CommandResult)


# 5/5 -----------------------------------------------------------------


def test_acceptance_5_transfer_no_platform_branch(
    local_session, fake_remote: FakeRemoteSession, tmp_path
):
    """§5/5：文件传输通过 push/pull/copy，调用者不写平台分支。"""
    src = tmp_path / "f"
    src.write_text("payload")
    # push：local -> fake_remote
    fake_remote.push(local_session.path(str(src)), fake_remote.path("/p"))
    # pull：fake_remote -> local
    fake_remote.pull(fake_remote.path("/p"), local_session.path(str(tmp_path / "back")))
    assert (tmp_path / "back").read_text() == "payload"


# 5/6 -----------------------------------------------------------------


def test_acceptance_6_stale_is_plain_function(local_session, tmp_path):
    """§5/6：``rpm.stale`` 是顶级函数并返回 ``bool``；无 ``Rule`` / ``target_count``。"""
    assert callable(rpm.stale)
    # 不允许出现旧 API
    assert not hasattr(rpm, "make")
    assert not hasattr(rpm, "Rule")
    src = tmp_path / "s"; src.write_text("x")
    tgt = tmp_path / "t"
    result = rpm.stale(
        local_session.path(str(tgt)),
        depends_on=local_session.path(str(src)),
    )
    assert isinstance(result, bool)


# 5/7 -----------------------------------------------------------------


def test_acceptance_7_no_integration_default(pytestconfig):
    """§5/7：默认 ``pytest`` 命令既不跑 integration，也不跑浏览器 e2e。"""
    ini = pytestconfig.getini("addopts") or ""
    addopts = " ".join(ini if isinstance(ini, list) else [ini])
    assert "not integration" in addopts
    assert "not e2e" in addopts, "浏览器 e2e 需要 playwright 二进制，默认套件必须排除"


# 5/8 -----------------------------------------------------------------


def test_acceptance_8_integration_scaffold_present():
    """§5/8：SSH/ADB/串口必须放在 ``tests/integration/`` 且带 integration 标记。"""
    root = Path(__file__).parent / "integration"
    assert root.is_dir(), "tests/integration/ must exist for §5/8"
    for name in ("test_ssh_session.py", "test_adb_session.py", "test_serial_session.py"):
        f = root / name
        assert f.is_file(), f"missing scaffold {name}"
        content = f.read_text(encoding="utf-8")
        assert "pytest.mark.integration" in content, (
            f"{name} must mark tests with @pytest.mark.integration"
        )


def test_acceptance_8_e2e_suite_is_marked():
    """§5/8：``tests/e2e/`` 下每个用例文件都必须带 e2e 标记。

    漏标一个文件，默认 ``pytest`` 就会去拉浏览器——在没装 chromium 的机器上直接
    红一片，正是这条分层要防的事。
    """
    root = Path(__file__).parent / "e2e"
    assert root.is_dir(), "tests/e2e/ must exist for §CORE-11 Web UI 端到端验证"
    files = sorted(root.glob("test_*.py"))
    assert files, "tests/e2e/ 至少要有一个用例文件"
    for f in files:
        content = f.read_text(encoding="utf-8")
        assert "pytest.mark.e2e" in content, (
            f"{f.name} must mark tests with pytest.mark.e2e"
        )


# 5/10 ----------------------------------------------------------------


def test_acceptance_10_core_installable_alone():
    """§5/10：核心库不依赖 Streamlit/NumPy/SciPy/pydub；导入 rpm 不触发这些包。"""
    import redpymake  # noqa: F401
    heavy = {"streamlit", "numpy", "scipy", "pydub"}
    loaded = heavy & set(sys.modules)
    assert not loaded, f"core import must not pull in {loaded}"


# 5/11 ----------------------------------------------------------------


def test_acceptance_11_run_wait_no_race(local_session, python_probe):
    """§5/11：``run().wait()`` 不会漏掉命令执行期间已产生的日志。"""
    result = local_session.run(*python_probe(
        "import sys, time; print('READY'); sys.stdout.flush(); time.sleep(0.05)"
    ))
    match = result.wait("READY", timeout=1.0)
    assert match.text == "READY"


# 5/12 ----------------------------------------------------------------


def test_acceptance_12_logs_readable_after_close(python_probe):
    """§5/12：会话关闭后，已收集的日志仍可读取和保存。"""
    sess = rpm.local()
    sess.run(*python_probe("print('HISTORY')"))
    sess.close()
    text = sess.logs.text()
    assert "HISTORY" in text
