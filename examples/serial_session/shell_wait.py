"""串口上等待自己认得出的一行。库不解释哪段日志是命令回复。

改 PORT，并把 PROMPT 换成你在终端里看到的那一行：

    pip install -e ".[serial]"
    python examples/serial_session/shell_wait.py

下面的 PROMPT 只适合「行尾是 # 或 $」的 Linux shell，并且故意允许路径变化：
`/ #` 和 `/home #` 都能匹配。不要写成 r"/ #"，那个匹配不到 `/home #`。
U-Boot 一类设备请换成自己的，例如 re.compile(r"=>\\s*$")。
"""

import re

import redpymake as rpm

PORT = "COM3"

# 示例，不是 redpymake 的一部分。换设备就改这里。
PROMPT = re.compile(
    r"^(?:[\w.+-]+@[\w.+-]+:)?"  # 可选 user@host:
    r"(?:[~/][\w./~+-]*)?"  # 可选路径：/、/home、~
    r"\s*[#$]\s*$"
)

with rpm.serial(PORT, baudrate=115200) as uart:
    cursor = uart.logs.cursor()
    uart.run("uname -a\n")
    uart.wait(PROMPT, since=cursor, timeout=10)
    print(uart.logs.text(since=cursor, channel="serial"))
