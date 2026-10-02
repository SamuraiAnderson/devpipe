# RedPyMake

[![CI](https://github.com/SamuraiAnderson/devpipe/actions/workflows/ci.yml/badge.svg)](https://github.com/SamuraiAnderson/devpipe/actions/workflows/ci.yml)

统一的 `Session` 接口，跨 local / ssh / adb / serial / wsl 执行命令、传输文件、收集日志。
Python 3.10+，核心零依赖。

## 安装

```bash
pip install -e .            # 核心
pip install -e ".[ssh]"     # paramiko
pip install -e ".[serial]"  # pyserial
pip install -e ".[web]"     # fastapi + uvicorn
pip install -e ".[dev]"     # pytest 与可选平台依赖
```

push / PR 到 `main` 时 [GitHub Actions](https://github.com/SamuraiAnderson/devpipe/actions) 会跑默认单测（不含 integration / e2e）。本地同样：

```bash
pytest                      # 默认单测
pytest --cov=redpymake      # 覆盖率门槛 80%
pytest -m integration       # 真实设备；需 RPM_TEST_* 环境变量
pytest -m e2e               # 浏览器；需 pip install -e ".[e2e]" 且 playwright install chromium
```

## 工厂

| 工厂 | 依赖 |
| --- | --- |
| `rpm.local()` | — |
| `rpm.ssh(host, *, user=None, port=22, password=None, key_filename=None)` | `[ssh]` |
| `rpm.adb(serial=None, *, adb_path=None)` | `adb` 在 PATH |
| `rpm.serial(port, *, baudrate=115200)` | `[serial]` |
| `rpm.wsl(distribution=None, *, user=None)` | Windows |

返回同一 `Session` 接口，`run` / `at` / `path` / `push` / `pull` / `wait` / `logs` 通用。

## 用法

```python
import redpymake as rpm

with rpm.ssh("192.168.1.10", user="root") as remote:
    print(remote.run("uname", "-a").stdout)
    print(remote.at("/tmp").run("pwd").stdout.strip())
```

### 经 SSH 连接设备

链式调用返回终点会话，命令、工作目录、路径和日志都属于终点：

```python
with rpm.ssh("lab-linux", user="tester").adb("DEVICE_SN") as android:
    print(android.run("getprop", "ro.product.model").stdout)
    android.push("firmware.bin", "/data/local/tmp/firmware.bin", timeout=60)

with rpm.ssh("lab-windows").wsl("Ubuntu") as linux:
    linux.at("/home/test/project").run("make", timeout=120)

with rpm.ssh("lab-linux").serial("/dev/ttyUSB0", newline="\r") as uart:
    uart.run("version").wait("OK", timeout=5)
    uart.run(b"\x00\xff")  # bytes 原样写入，不追加 newline
```

SSH 自动检测 POSIX / Windows，也可传 `platform="posix"` 或 `"windows"`。
远端需具备相应工具：ADB、Windows 的 WSL，或 Python 3.10+ 和 pyserial。
串口可用 `python_path` 指定远端解释器；本机只需 `[ssh]`，无需本机 pyserial。
Windows 后端使用 Windows PowerShell 与 .NET，需要允许执行命令和 `Add-Type`。

远端无需安装 RedPyMake。串口桥接源码从标准输入发送，关闭或客户端租约到期后释放端口；
文件传输使用远端登录用户的临时目录并在完成后清理，清理失败会报错。
UART 不分配 PTY，保留字节及顺序；读取块边界、字节间隔和电气信号不属于透传保证。
UART `run()` 成功表示驱动接受写入，设备是否执行成功仍需 `wait()` 确认。
不会在断线后自动重连并重发命令。

一条独占链关闭终点就会关闭 SSH。多个设备共享宿主时使用外层作用域：

```python
with rpm.ssh("lab-linux") as host:
    with host.adb("DEVICE_SN") as android, host.serial("/dev/ttyUSB0") as uart:
        android.run("echo", "ready")
```

Workspace 会按宿主和设备身份复用链式会话，退出 Workspace 时统一释放。
首版支持 SSH → ADB / serial / WSL；其他嵌套组合会明确报 `UnsupportedOperationError`。

### 命令执行

```python
res = remote.run("make", "-j8")               # 非零抛 CommandError
res = remote.run("test", "-f", "x", check=False)   # 手动判断 res.ok
remote.run("cat a | grep b", shell=True)      # shell=True 时不接受额外 argv
```

### 传输

```python
here = rpm.local()
remote.push(here.path("dist/app"), remote.path("/opt/app"))
remote.pull(remote.path("/var/log/x.log"), here.path("logs/x.log"))
```

### 等日志

```python
import re

with rpm.serial("COM3", baudrate=115200) as uart:
    uart.run("reboot\r").wait("U-Boot", timeout=10).wait(
        re.compile(r"login:\s*$"), timeout=60
    )
    uart.wait(["READY", "FAILED"], timeout=5)
    uart.run(b"\x01").wait(b"\x06", timeout=1)
```

`run(...).wait(...)` 用 run 前保存的游标搜索，不会漏掉命令期间产生的匹配。`.wait().wait()` 从上一命中之后继续。`wait(["A", "B"])` 为 OR（谁先出现谁赢）。

### 过时判断

```python
build = remote.at("/workspace")
target = build.path("build/app")
sources = [build.path("src/main.c"), build.path("src/utils.c")]

if rpm.stale(target, depends_on=sources, name="build_app"):
    build.run("cmake", "--build", "build", "-j8")
```

### 脚本对象

```python
import logging

with rpm.script("nightly", dump_on_error="logs/"):
    logging.getLogger("build").info("start")
    here = rpm.local()
    remote = rpm.ssh("10.0.0.1")
    remote.run("make", "-j8")
```

作用域内的会话自动登记；异常时把业务 `logging` + 每个会话的输出打包写入 `logs/nightly-<ts>/`；原异常照抛。

### CLI 与 Web UI

命名为 `rpm_*.py` 的脚本可被发现：

```bash
redpymake discover examples/
redpymake run rpm_build.py --sink run.ndjson
redpymake report run.ndjson -o run.html
redpymake serve examples/wsl_session         # 127.0.0.1:8765
redpymake targets                            # 列出 pyproject + 本机覆盖，不建连
redpymake exec @board ls /workspace          # 选项写在目标名前面
```

目标声明在 `[tool.redpymake.targets.<name>]`，本机地址放 `.redpymake/targets.toml`（已 gitignore）。脚本里用 `rpm.target("board")` 拿到同一个目标。

`redpymake serve` 背后是 `rpm.workspace(...)`，同一目标在多次 run 之间复用连接。

## 示例

- [`examples/quickstart/`](examples/quickstart/README.md) — 最小 SSH 示例
- [`examples/wsl_session/`](examples/wsl_session/README.md) — 传输 / stale / wait / script / 长流水线

## 接口分层

| 层次 | 主要接口 | 说明 |
| --- | --- | --- |
| 一级：核心执行 | `session.at`, `session.run`, `session.path` | 会话、工作目录、命令、路径 |
| 二级 A：文件传输 | `session.push`, `session.pull`, `rpm.copy` | `push` / `pull` 有方向；`copy` 是顶级函数 |
| 二级 B：日志与等待 | `session.logs`, `session.wait`, `run(...).wait` | 自动收集、订阅、tag、等待 |
| 二级 C：过时判断 | `rpm.stale(target, depends_on=...)` | 增量优化辅助 |
| 二级 D：脚本对象 | `rpm.script(...)` | 多会话日志合流、异常自动落盘 |
| 二级 E：脚本发现与实时日志 | `rpm.discover(...)`, `rpm.target(...)`、live sink | 脚本发现、命名目标、实时日志输出 |
| 二级 F：Workspace 与 Web UI | `rpm.workspace(...)`、`redpymake serve` | 连接复用、脚本队列、Web UI |
| 二级 G：会话组合与连接路由 | `rpm.ssh(...).adb(...)` / `.serial(...)` / `.wsl(...)` | 构造终点会话、管理上游依赖与生命周期 |

功能分级与连接跳数、开发优先级分别定义；当前规格未定义三级功能。

所有异常继承 `RedPyMakeError`，不会漏出 paramiko 原生异常或裸 `RuntimeError`。

## 容易踩的坑

- `run()` 默认 `check=True`：非零退出立即抛 `CommandError`；`check=False` 才返回结果让调用者判断。
- `at()` 返回**新的视图**并共享同一连接与日志，不修改原会话；只想覆盖一次用 `run(cwd=...)`。
- `session.wait(pattern)` 默认从"下一条"开始扫，匹配不到调用之前的历史；覆盖命令执行期间的输出用 `run(...).wait(pattern)`。顺序再用 `.wait()` 链；OR 用 `wait(["A", "B"])`。
- 串口 `run("reboot")` 默认**不**追加 `\r\n`；需要行结束符请写 `run("reboot\r")`。二进制用 `run(bytes)` / `wait(bytes)`。
- 会话关闭后仍可读取已收集的日志，但会拒绝新的 `run` / `push` / `wait`。
- `rpm.wsl()` 只校验 `wsl.exe` 存在，发行版没装会延迟到首次 `run()` 时以 `CommandError` 报出来。

## 完整规格

[`doc/core-lib-requirements.md`](doc/core-lib-requirements.md)

## 给 LLM / Agent

- [`AGENTS.md`](AGENTS.md) — 硬约束、五条反直觉语义、改码流程
- [`doc/api-cheatsheet.md`](doc/api-cheatsheet.md) — 常用公开符号的真实签名与字段表，一页
- [`llms.txt`](llms.txt) — 文档索引，按 [llmstxt.org](https://llmstxt.org/) 约定
