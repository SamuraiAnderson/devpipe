"""
TFTP 服务端部署测试脚本
测试内容:
  1. 本机 (LocalHost) 准备 TFTP 根目录与测试文件
  2. 启动 TFTP 服务端 (TftpdServer)
  3. 使用 tftpy 客户端验证文件下载
  4. 停止服务并清理
"""

import logging
import os
import tempfile
import time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TFTP_HOST = os.getenv("TFTP_HOST", "127.0.0.1")
TFTP_PORT = int(os.getenv("TFTP_PORT", "6969"))


def make_test():
    from src.localhost import LocalHost
    from src.tftp_server import TftpdServer

    local = LocalHost()
    tmp_dir = tempfile.mkdtemp(prefix="rpm_tftp_")

    try:
        # ── 1. 准备 TFTP 根目录 ──
        log.info("===== 1. 准备 TFTP 根目录 =====")
        local.pwd = tmp_dir
        log.info("临时目录: %s", tmp_dir)

        test_file = os.path.join(tmp_dir, "firmware.bin")
        test_content = b"FAKE_FIRMWARE_DATA_v1.0\n"
        with open(test_file, "wb") as f:
            f.write(test_content)
        log.info("已创建测试文件: %s (%d bytes)", test_file, len(test_content))

        # ── 2. 启动 TFTP 服务 ──
        log.info("===== 2. 启动 TFTP 服务 =====")
        tftp = TftpdServer(root_dir=tmp_dir, host=TFTP_HOST, port=TFTP_PORT)
        tftp.start()
        time.sleep(0.5)
        log.info("TFTP 服务运行中  %s:%d", TFTP_HOST, TFTP_PORT)

        # ── 3. 客户端下载验证 ──
        log.info("===== 3. 客户端下载验证 =====")
        import tftpy
        client = tftpy.TftpClient(TFTP_HOST, TFTP_PORT)
        download_path = os.path.join(tmp_dir, "downloaded.bin")
        client.download("firmware.bin", download_path)

        with open(download_path, "rb") as f:
            downloaded = f.read()
        assert downloaded == test_content, f"内容不一致: {downloaded!r}"
        log.info("下载验证通过: firmware.bin (%d bytes)", len(downloaded))

        # ── 4. 停止服务 ──
        log.info("===== 4. 停止 TFTP 服务 =====")
        tftp.stop()

        log.info("===== 全部测试通过 =====")

    except Exception as e:
        log.error("测试失败: %s", e, exc_info=True)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("临时目录已清理: %s", tmp_dir)


if __name__ == "__main__":
    make_test()
