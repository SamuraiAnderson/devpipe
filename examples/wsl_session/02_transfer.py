"""push / pull 的字节往返，以及 Windows 路径 → /mnt/<drive>/ 的自动映射。

覆盖：§CORE-04（跨会话文件传输）。

运行前提：Windows 上已安装 WSL。脚本使用系统临时目录，退出前自动清理远端探针。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import redpymake as rpm


def main() -> None:
    with tempfile.TemporaryDirectory() as raw_dir:
        d = Path(raw_dir)
        src = d / "up.bin"
        dst = d / "down.bin"
        src.write_bytes(b"redpymake-wsl-demo")

        with rpm.wsl() as sh, rpm.local() as here:
            remote = sh.path("/tmp/redpymake-wsl-demo.bin")

            # 关键点：直接把 Windows 路径（例如 C:\Users\...\up.bin）交给 push，
            # WslSession 内部会把它换成 /mnt/c/Users/.../up.bin，不需要手工转。
            sh.push(here.path(str(src)), remote)
            print(f"pushed:  {src} -> {remote}")

            sh.pull(remote, here.path(str(dst)))
            print(f"pulled:  {remote} -> {dst}")

            sh.run("rm", "-f", str(remote))

        original = src.read_bytes()
        roundtripped = dst.read_bytes()
        print(f"src bytes = {original!r}")
        print(f"dst bytes = {roundtripped!r}")
        assert original == roundtripped, "round-trip 字节不一致"
        print("OK: bytes match")


if __name__ == "__main__":
    main()
