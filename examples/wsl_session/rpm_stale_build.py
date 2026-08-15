"""用 rpm.stale 做增量：只有源比目标新（或目标不存在）才重新"构建"。

覆盖：§CORE-05（过时判断辅助）。

运行前提：Windows 上已安装 WSL。
第一次运行会"构建"，紧接着第二次运行应报告 fresh 并跳过。
"""

from __future__ import annotations

import redpymake as rpm


def build_once(work: rpm.Session) -> None:
    target = work.path("build/app")
    source = work.path("src/main.c")

    if rpm.stale(target, depends_on=source, name="build_app"):
        print("[stale] rebuilding build/app")
        # 演示用："构建" = 把 main.c 拷成产物；真实工程可换成 make/cmake
        work.run("sh", "-c", "cp src/main.c build/app")
    else:
        print("[fresh] skip build (target newer than source)")


def main() -> None:
    with rpm.script("wsl-stale-build"):
        with rpm.wsl() as sh:
            work = sh.at("/tmp/rpm-wsl-example")

            # 准备工程骨架（幂等）
            sh.run("mkdir", "-p", "/tmp/rpm-wsl-example/src",
                   "/tmp/rpm-wsl-example/build")
            sh.run(
                "sh",
                "-c",
                'test -f /tmp/rpm-wsl-example/src/main.c '
                '|| printf "int main(void){return 0;}\\n" '
                '   > /tmp/rpm-wsl-example/src/main.c',
            )

            # 第一次：产物可能不存在 → stale
            print("--- first pass ---")
            build_once(work)

            # 第二次：产物比源新 → fresh
            print("--- second pass ---")
            build_once(work)

            # 让源比目标晚 2 秒（stat -c %Y 是秒精度，不 sleep 会同秒 → 仍 fresh）。
            # 这里演示"源被修改后再次判定为 stale"。
            work.run("sh", "-c", "sleep 2 && touch src/main.c")
            print("--- after touching src/main.c ---")
            build_once(work)


if __name__ == "__main__":
    main()
