# 串口：自己认日志里的那一行

[`shell_wait.py`](shell_wait.py) 演示调用方自己写模式，再用 `run()` 写出、`wait()` 等待。

库不把「提示符之前的日志」定义成命令回复。串口上常有无关日志一直流入，哪些行有意义只有脚本知道。

## 跑之前改两行

```python
PORT = "COM3"
PROMPT = re.compile(r"...")  # 改成你在终端里真实看到、并且认得出的那一行
```

然后：

```bash
pip install -e ".[serial]"
python examples/serial_session/shell_wait.py
```

## `r"/ #"` 为什么会漏

有的 shell 提示符带当前目录：在 `/` 时是 `/ #`，`cd /home` 之后变成 `/home #`。

`r"/ #"` 要求斜杠紧挨着空格。`/home #` 里没有这个子串，所以 cwd 一变就再也等不到。示例里的正则允许路径出现在 `#` / `$` 前面。它只适合这一类提示符；U-Boot 的 `=>`、启动日志里的某一行，都要换成你自己认得出的模式。
