"""最小 SSH 示例：连上一台机器，跑一条命令，拿回结果。

改下面三个常量就能跑：

    pip install -e ".[ssh]"
    python examples/quickstart/hello_ssh.py
"""

import redpymake as rpm

HOST = "192.168.1.10"
USER = "root"
PASSWORD = "your-password"

with rpm.ssh(HOST, user=USER, password=PASSWORD) as remote:
    res = remote.run("uname", "-a")
    print(res.stdout.strip())

    # at() 返回一个新视图，共享同一条连接，不影响 remote 本身
    tmp = remote.at("/tmp")
    print(tmp.run("pwd").stdout.strip())


# ---------------------------------------------------------------------------
# 接下来可以试的三件事
#
# 1) 用密钥代替密码：
#        rpm.ssh(HOST, user=USER, key_filename="~/.ssh/id_rsa")
#
# 2) 不用检查返回码。run() 默认 check=True，命令失败会直接抛 CommandError，
#    所以上面不需要写 if res.returncode != 0。想自己判断就传 check=False，
#    然后看 res.ok。
#
# 3) 换一个运行环境，下面的代码一个字都不用改：
#        rpm.local()                      本机
#        rpm.wsl()                        Windows 上的 WSL
#        rpm.adb("emulator-5554")         Android 设备
#        rpm.serial("COM3", baudrate=115200)   串口
#
#    手边没有 SSH 机器？把上面的 rpm.ssh(...) 换成 rpm.local()，
#    再把 "uname" 换成本机有的命令，就能立刻看到效果。
#
# 更多用法见 examples/README.md 和仓库根的 README.md。
# ---------------------------------------------------------------------------
