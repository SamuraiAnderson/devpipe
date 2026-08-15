"""在命令输出里等待指定模式：``run().wait(pattern)`` 使用命令前保存的游标。

覆盖：§CORE-06（日志等待）。

关键点：
- ``session.wait(pattern)`` 默认从"下一条"开始扫描，**不会**匹配调用之前的历史，
  所以先 ``run()`` 再 ``session.wait()`` 会漏掉命令期间的输出。
- 正确姿势：``result = sh.run(...); result.wait(pattern)``。``CommandResult`` 内部
  持有命令启动前保存的 ``LogCursor``，扫描起点就是 run 之前，命令期间的行不会漏。

运行前提：Windows 上已安装 WSL。
"""

from __future__ import annotations

import re

import redpymake as rpm
from redpymake import LogWaitTimeoutError


def main() -> None:
    with rpm.script("wsl-wait-logs"):
        with rpm.wsl() as sh:
            # 用一个会先吐几行 tick、最后吐 READY 的短命令；执行时长在秒级内。
            result = sh.run(
                "sh",
                "-c",
                "for i in 1 2 3; do echo tick-$i; done; echo READY; echo bye 1>&2",
            )

            # 直接扫命令输出中的 READY：wait 会从 run 之前的游标开始扫，稳。
            match = result.wait(re.compile(r"^READY$"), timeout=5)
            print(f"matched: {match.text!r}  elapsed={match.elapsed:.3f}s")
            print(f"record.stream = {match.record.stream}")

            # channel 参数示例：只在 stderr 找 'bye'。
            match_err = result.wait("bye", timeout=5, channel="stderr")
            print(f"matched on stderr: {match_err.text!r}")

            # 反例演示：如果只有历史数据，直接调用 session.wait 会 timeout。
            try:
                sh.wait("READY", timeout=0.5)
            except LogWaitTimeoutError as exc:
                print(f"(expected) session.wait timed out: {exc}")


if __name__ == "__main__":
    main()
