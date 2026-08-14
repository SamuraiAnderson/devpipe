"""WSL 会话入门：构造、切换工作目录、读取命令结果与日志。

覆盖：§CORE-01（会话构造与元信息）、§CORE-02（run + at）、§CORE-06（日志收集）。

运行前提：Windows 上已安装 WSL 且至少存在一个默认发行版。
"""

from __future__ import annotations

import redpymake as rpm


def main() -> None:
    with rpm.wsl() as sh:
        print(f"kind={sh.kind}  label={sh.label}")
        print(f"path_style={sh.path_style}  default_cwd={sh.default_cwd}")

        # 位置参数模式，安全传参不解析 shell 元字符
        r = sh.run("uname", "-a")
        print("uname -a:", r.stdout.strip())
        print(f"  ok={r.ok}  duration={r.duration:.3f}s")

        # at() 返回新的视图，共享同一连接与日志缓冲
        tmp = sh.at("/tmp")
        print("pwd in /tmp:", tmp.run("pwd").stdout.strip())

        # shell=True 支持管道等；此时不能再传额外位置参数
        r2 = sh.run("echo hi && echo boom 1>&2", shell=True, check=False)
        print("shell mode -> ok:", r2.ok, "stdout:", r2.stdout.strip(),
              "stderr:", r2.stderr.strip())

        # 会话日志：run 期间的 stdout/stderr 都被逐行收集
        records = sh.logs.records()
        print(f"\n总共收集到 {len(records)} 条日志，最后 5 条：")
        for rec in records[-5:]:
            print(f"  [{rec.event}/{rec.stream}] {rec.message}")


if __name__ == "__main__":
    main()
