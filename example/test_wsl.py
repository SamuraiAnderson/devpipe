"""
WSL + 本机 跨平台测试脚本
测试内容:
  1. 本机 (LocalHost) 基本操作
  2. WSL (Linux via SSH) 连接与 shell 命令
  3. 本机 ↔ WSL 文件传输 (push / pull)
  4. UFile 跨平台路径与时间戳
  5. make_style 增量执行
"""

import logging
import os
import sys
import tempfile
import time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

WSL_HOST = os.getenv("WSL_HOST", "localhost")
WSL_USER = os.getenv("WSL_USER", "lain")


def make_test():
    from src.localhost import LocalHost
    from src.linux_controller import Linux
    from src.file import UFile
    from src.make_style import make_style_decorate
    from src.BaseControl import RemoteError

    local = LocalHost()
    linux = None
    tmp_dir = tempfile.mkdtemp(prefix="rpm_test_")

    try:
        # ── 1. LocalHost 基本操作 ──
        log.info("===== 1. LocalHost 基本操作 =====")
        local.pwd = tmp_dir
        log.info("pwd = %s", local.pwd)
        assert local.host == "localhost"
        assert local.name == "nt"
        log.info("LocalHost 初始化成功  host=%s  name=%s", local.host, local.name)

        hello_path = os.path.join(tmp_dir, "hello.txt")
        with open(hello_path, "w", encoding="utf-8") as f:
            f.write("hello from local\n")
        assert local.file_exist(hello_path)
        ts = local.get_file_timestamp(hello_path)
        log.info("本地文件时间戳: %s  (%s)", ts, time.ctime(ts))

        # ── 2. 连接 WSL ──
        log.info("===== 2. 连接 WSL (SSH %s@%s) =====", WSL_USER, WSL_HOST)
        linux = Linux(WSL_HOST, WSL_USER)
        log.info("WSL 连接成功  host=%s  name=%s", linux.host, linux.name)

        _, stdout, _ = linux.shell("uname -a")
        log.info("WSL uname: %s", stdout.strip())

        _, stdout, _ = linux.shell("echo $HOME")
        wsl_home = stdout.strip()
        log.info("WSL HOME: %s", wsl_home)

        # ── 3. WSL shell 命令 ──
        log.info("===== 3. WSL Shell 命令测试 =====")

        linux.pwd = "/tmp"
        log.info("WSL pwd 设置为 /tmp")

        _, stdout, _ = linux.shell("pwd")
        log.info("WSL pwd 输出: %s", stdout.strip())

        _, stdout, _ = linux.shell("echo 'hello from WSL'")
        log.info("WSL echo: %s", stdout.strip())

        _, stdout, _ = linux.shell("date +%s")
        log.info("WSL 时间戳: %s", stdout.strip())

        # ── 4. 文件传输 Local → WSL → Local ──
        log.info("===== 4. 文件传输测试 =====")

        wsl_remote_path = "/tmp/rpm_test_hello.txt"
        linux.push(hello_path, wsl_remote_path)
        log.info("push 成功: %s → %s", hello_path, wsl_remote_path)

        assert linux.file_exist(wsl_remote_path)
        log.info("WSL 文件存在: %s", wsl_remote_path)

        wsl_ts = linux.get_file_timestamp(wsl_remote_path)
        log.info("WSL 文件时间戳: %s  (%s)", wsl_ts, time.ctime(wsl_ts))

        pull_path = os.path.join(tmp_dir, "hello_from_wsl.txt")
        linux.pull(wsl_remote_path, pull_path)
        log.info("pull 成功: %s → %s", wsl_remote_path, pull_path)

        with open(pull_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert content == "hello from local\n", f"内容不一致: {content!r}"
        log.info("文件内容校验通过")

        # ── 5. UFile 跨平台操作 ──
        log.info("===== 5. UFile 跨平台操作 =====")

        local_file = UFile(local, hello_path)
        wsl_file = UFile(linux, wsl_remote_path)
        log.info("本地 UFile: %s  exist=%s", local_file, local_file.exist)
        log.info("WSL  UFile: %s  exist=%s", wsl_file, wsl_file.exist)

        log.info("本地文件时间戳: %s", local_file.get_timestamp())
        log.info("WSL 文件时间戳: %s", wsl_file.get_timestamp())

        # ── 6. make_style 增量执行测试 ──
        log.info("===== 6. make_style 增量执行测试 =====")
        call_count = 0

        @make_style_decorate
        def fake_build(target: UFile, src: UFile):
            nonlocal call_count
            call_count += 1
            log.info("fake_build 执行: %s ← %s", target, src)
            content = f"built at {time.time()}\n"
            with open(target.get_abs_path(), "w", encoding="utf-8") as f:
                f.write(content)

        src_path = os.path.join(tmp_dir, "source.txt")
        tgt_path = os.path.join(tmp_dir, "target.txt")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write("source v1\n")

        src_file = UFile(local, src_path)
        tgt_file = UFile(local, tgt_path)

        fake_build(tgt_file, src_file)
        assert call_count == 1, "首次构建应执行"
        log.info("首次构建: 已执行 (符合预期)")

        time.sleep(1.1)
        fake_build(tgt_file, src_file)
        assert call_count == 1, "目标较新时应跳过"
        log.info("重复构建: 已跳过 (符合预期)")

        time.sleep(1.1)
        with open(src_path, "w", encoding="utf-8") as f:
            f.write("source v2\n")

        src_file = UFile(local, src_path)
        tgt_file = UFile(local, tgt_path)
        fake_build(tgt_file, src_file)
        assert call_count == 2, "源更新后应重新执行"
        log.info("源更新后构建: 已执行 (符合预期)")

        # ── 7. 清理 WSL 临时文件 ──
        log.info("===== 7. 清理 =====")
        linux.shell(f"rm -f {wsl_remote_path}")
        log.info("WSL 临时文件已清理")

        log.info("===== 全部测试通过 =====")

    except RemoteError as e:
        log.error("测试失败: %s", e, exc_info=True)
        sys.exit(1)
    except AssertionError as e:
        log.error("断言失败: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        # if linux is not None:
        #     linux.close()
        #     log.info("WSL 连接已关闭")
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("本地临时目录已清理: %s", tmp_dir)


if __name__ == "__main__":
    make_test()
