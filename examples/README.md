# RedPyMake Examples

这里放**面向读者**的可运行示例，与 `tests/integration/` 的最小契约测试形成互补：
测试用来固定行为，例子用来教怎么用。

| 目录 | 主题 | 前置条件 |
| --- | --- | --- |
| [`wsl_session/`](wsl_session/README.md) | WSL 会话：命令 / 传输 / 增量构建 / 日志等待 / 脚本对象 | Windows + 已安装 WSL |

## 运行

先按 [`../README.md`](../README.md) 安装：

```bash
pip install -e .
```

然后直接运行任一脚本：

```bash
python examples/wsl_session/01_hello.py
```

所有示例都是**幂等**的：反复运行不会污染本机，除非脚本明确写了持久输出（在头部注释里会说明）。
