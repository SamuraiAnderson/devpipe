"""跨端长任务流水线：本机 (``rpm.local``) + WSL (``rpm.wsl``) 协同示例。

覆盖：§CORE-01 / §CORE-02 / §CORE-04 / §CORE-06 / §CORE-09；配套 §CORE-11 Web UI
下的实时时间轴 & 回放（`redpymake serve examples\\wsl_session` 后从 Web UI 触发）。

运行前提：
- **Windows** + 已安装 **WSL**（`wsl -l -v` 至少能看到一个发行版）；
- 仓库根 ``pip install -e .``；Web UI 场景另装 ``pip install -e ".[web]"``。

演示要点：
- **两个 session 交替产出**：本机侧算数据 / 写 manifest；WSL 侧长任务式吐 build 日志。
- **流式日志**：``sh.run("bash", "-c", "for ...; do echo ...; sleep 0.4; done")`` 会在
  子进程运行期间**逐行**打到会话 buffer；Web UI 里 Live 模式下会看到条目一条条冒出。
- **push/pull 往返 + 交叉校验**：Windows 路径经 WslSession 自动映射到
  ``/mnt/<drive>/...``；pull 回本机后二进制比对。
- **``run().wait(pattern)`` 用命令启动前的游标**扫历史，可靠地捕捉 marker。
- **user_log 与命令输出同轨道**：``logging`` 模块产的记录（``event=user_log``）
  与命令的 ``command_output`` 都按 timestamp 稳定排序，回放时序不会错乱。

预计耗时：约 20 秒（够看清 Live 追流；跑完后侧栏点历史 Run 就能进入 Replay）。
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import redpymake as rpm

log = logging.getLogger("pipeline")


def _stage(title: str) -> None:
    """在时间轴里插一条清晰的分段标记（3 条 user_log，肉眼容易定位阶段边界）。"""
    log.info("=" * 60)
    log.info("STAGE  %s", title)
    log.info("=" * 60)


def main() -> None:
    with rpm.script("wsl-long-pipeline"):
        with tempfile.TemporaryDirectory(prefix="rpm-pipeline-") as raw:
            workdir = Path(raw)
            manifest_local = workdir / "manifest.txt"
            manifest_pulled = workdir / "manifest.pulled.txt"
            manifest_remote = "/tmp/rpm-pipeline-manifest.txt"

            # 一次拿到两个 session；出块后 workspace 内会共享复用，独立跑则整体关闭。
            with rpm.local() as here, rpm.wsl() as wsl:
                _stage("1/5 探针：确认两端环境")
                log.info("local  kind=%s label=%s", here.kind, here.label)
                log.info("wsl    kind=%s label=%s", wsl.kind, wsl.label)
                here.run(
                    "python",
                    "-c",
                    "import platform, sys; print('local python', platform.python_version(), 'exe', sys.executable)",
                )
                wsl.run(
                    "bash",
                    "-c",
                    "uname -a; (lsb_release -a 2>/dev/null || sed -n '1,3p' /etc/os-release)",
                )

                _stage("2/5 本机侧数据生成：8 拍慢速吐（每拍 0.4s，便于观察）")
                for i in range(1, 9):
                    log.info("local tick %d/8", i)
                    here.run(
                        "python",
                        "-c",
                        (
                            f"import time, sys; "
                            f"print('gen record {i}/8'); sys.stdout.flush(); "
                            f"time.sleep(0.4); "
                            f"print('  -> hash', hex(hash(('rpm', {i})) & 0xFFFF))"
                        ),
                    )
                manifest_local.write_text(
                    "\n".join(f"record-{i}" for i in range(1, 9)) + "\n",
                    encoding="utf-8",
                )
                log.info(
                    "manifest written: %s (%d bytes)",
                    manifest_local,
                    manifest_local.stat().st_size,
                )

                _stage("3/5 push 本机 manifest 到 WSL 侧 /tmp")
                # 关键：Windows 路径直接给 push，WslSession 内部换成 /mnt/<drive>/...
                wsl.push(here.path(str(manifest_local)), wsl.path(manifest_remote))
                log.info("pushed %s -> %s", manifest_local, manifest_remote)
                # 让 WSL 打印一次，肉眼确认已到位
                wsl.run("bash", "-c", f"wc -l {manifest_remote}; head -n 3 {manifest_remote}")

                _stage("4/5 WSL 侧长任务：流式 10 步 build + wait(READY)")
                # 一条命令，10 行逐渐吐出；每行 stdout 都会作为一条 record 出现在时间轴上。
                wsl.run(
                    "bash",
                    "-c",
                    (
                        "echo '[build] starting'; "
                        "for i in 01 02 03 04 05 06 07 08 09 10; do "
                        "  echo \"[build] step-$i ok\"; "
                        "  sleep 0.35; "
                        "done; "
                        f"echo '[build] verify manifest:'; cat {manifest_remote}; "
                        "echo '[build] done'"
                    ),
                )

                # ``run().wait()`` 用命令启动前保存的 LogCursor，稳定捕获 READY 标记。
                r = wsl.run(
                    "bash",
                    "-c",
                    (
                        "for i in 1 2 3 4 5; do "
                        "  echo warmup-$i; "
                        "  sleep 0.25; "
                        "done; "
                        "echo READY"
                    ),
                )
                match = r.wait("READY", timeout=5)
                log.info(
                    "wait matched: %r on stream=%s (elapsed=%.3fs)",
                    match.text,
                    match.record.stream,
                    match.elapsed,
                )

                _stage("5/5 pull 回本机 + 二进制交叉校验")
                wsl.pull(wsl.path(manifest_remote), here.path(str(manifest_pulled)))
                a = manifest_local.read_bytes()
                b = manifest_pulled.read_bytes()
                assert a == b, "roundtrip bytes mismatch"
                log.info("roundtrip OK: %d bytes identical", len(a))

                # 收尾：清远端 + 打两端日志计数（回放时可以在末尾快速核对）
                wsl.run("rm", "-f", manifest_remote)
                log.info("local session records = %d", len(here.logs.records()))
                log.info("wsl   session records = %d", len(wsl.logs.records()))
                log.info("pipeline finished at wall=%s", time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    main()
