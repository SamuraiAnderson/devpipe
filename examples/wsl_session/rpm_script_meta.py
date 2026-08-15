"""把一段 WSL 流程包成 ``rpm.script(...)``：自动收集日志、tag、异常快照。

覆盖：§CORE-09（脚本对象与日志分流）。

要点：
- 在 ``with rpm.script(name) as run:`` 块内构造的会话会**自动 attach**
  （通过 ContextVar），无需手工调用 ``run.attach(sess)``。
- ``sh.logs.tag(**fields)`` 是 ``SessionLogs`` 上的上下文管理器，用于给这个 with
  块内产生的日志附加结构化字段。
- ``run.snapshot()`` 返回稳定排序的只读快照；``rpm.script(..., dump_on_error=<path>)``
  在异常出块时把整份记录落盘。

运行前提：Windows 上已安装 WSL。默认会在系统临时目录写一个错误快照演示文件。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import redpymake as rpm


def main() -> None:
    with rpm.script("wsl-demo") as run:
        with rpm.wsl() as sh:  # 自动 attach 到当前 script
            # 用 tag 给这个块内的日志附加统一标签（例如 phase / build_id）
            with sh.logs.tag(phase="smoke", build_id="local-1"):
                sh.run("uname", "-r")
                sh.run("sh", "-c", "echo done")

        snap = run.snapshot()
        print(f"script name = {snap.name}")
        print(f"records     = {len(snap.records)}")
        print(f"sessions    = {[(s.kind, s.label) for s in snap.sessions]}")
        # 快照里的每条记录都有稳定的 (timestamp, session_id, sequence) 排序
        tagged = [r for r in snap.records if r.fields.get("phase") == "smoke"]
        print(f"tagged records = {len(tagged)} (phase=smoke)")

    # ---- 额外演示：dump_on_error 落盘 ----
    with tempfile.TemporaryDirectory() as raw_dir:
        dump_path = Path(raw_dir) / "wsl-demo-crash.log"
        try:
            with rpm.script("wsl-demo-crash", dump_on_error=str(dump_path)) as run:
                with rpm.wsl() as sh:
                    sh.run("echo", "before-boom")
                    raise RuntimeError("boom, 用户代码异常")
        except RuntimeError as exc:
            print(f"(expected) caught: {exc}")

        # dump_on_error 已经把整份日志落到 dump_path
        print(f"crash dump at {dump_path}")
        print("---- head of dump ----")
        for line in dump_path.read_text(encoding="utf-8").splitlines()[:8]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
