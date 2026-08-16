# RedPyMake Examples

面向读者的可运行示例，与 `tests/integration/` 的最小契约测试互补：测试用来固定行为，
例子用来教怎么用。

## 从哪开始

| 顺序 | 目录 | 学到什么 | 前置条件 |
| --- | --- | --- | --- |
| 1 | [`quickstart/`](quickstart/README.md) | 20 行连上一台机器跑命令，没有任何样板 | 改三个常量；或换 `rpm.local()` 直接跑 |
| 2 | [`wsl_session/`](wsl_session/README.md) | 文件传输、增量构建、日志等待、脚本对象、跨端长流水线 | Windows + 已安装 WSL |

先跑 `quickstart/`，确认库能用；再按需去 `wsl_session/` 挑感兴趣的专题看。
`wsl_session/` 里的代码换个工厂（`rpm.ssh` / `rpm.adb`）就能用在别的环境上。

## 运行

先按 [`../README.md`](../README.md) 安装：

```bash
pip install -e ".[ssh]"
```

直接运行任一脚本：

```bash
python examples/quickstart/hello_ssh.py
python examples/wsl_session/rpm_hello.py
```

`wsl_session/` 下的脚本都遵循 `rpm_*.py` 发现约定，所以也可以走 CLI 或 Web UI：

```bash
redpymake discover examples/                 # 列出所有可运行脚本
redpymake run examples/wsl_session/rpm_hello.py --sink runs/hello.ndjson
redpymake report runs/hello.ndjson -o hello.html
redpymake serve examples/wsl_session         # Web UI，默认 127.0.0.1:8765
```

`quickstart/hello_ssh.py` 故意**不**用这个命名，用来说明那套约定是可选的 ——
只有需要从 Web UI 触发时才用得上。

所有示例都是**幂等**的：反复运行不会污染本机，除非脚本明确写了持久输出（在头部
注释里会说明）。
