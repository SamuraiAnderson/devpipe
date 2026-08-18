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

### 命令

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
    uart.run("reboot").wait("U-Boot", timeout=10)
    uart.wait(re.compile(r"login:\s*$"), timeout=60)
```

`run(...).wait(...)` 用 run 前保存的游标搜索，不会漏掉命令期间产生的匹配。

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
```

`redpymake serve` 背后是 `rpm.workspace(...)`，同一目标在多次 run 之间复用连接。

## 示例

- [`examples/quickstart/`](examples/quickstart/README.md) — 最小 SSH 示例
- [`examples/wsl_session/`](examples/wsl_session/README.md) — 传输 / stale / wait / script / 长流水线

## 接口分层

| 层次 | 主要接口 | 说明 |
| --- | --- | --- |
| 一级：核心执行 | `session.at`, `session.run`, `session.path` | 会话、工作目录、命令、路径 |
| 二级 A：文件传输 | `session.push`, `session.pull`, `session.copy` | 方向明确的便利接口，底层统一为 copy |
| 二级 B：日志与等待 | `session.logs`, `session.wait`, `run(...).wait` | 自动收集、订阅、tag、等待 |
| 二级 C：过时判断 | `rpm.stale(target, depends_on=...)` | 增量优化辅助 |
| 二级 D：脚本对象 | `rpm.script(...)` | 多会话日志合流、异常自动落盘 |
| 二级 E：工作区与可视化 | `rpm.workspace(...)`, `rpm.discover(...)` | 连接复用、脚本发现、Web UI 后端 |

所有异常继承 `RedPyMakeError`，不会漏出 paramiko 原生异常或裸 `RuntimeError`。

## 容易踩的坑

- `run()` 默认 `check=True`：非零退出立即抛 `CommandError`；`check=False` 才返回结果让调用者判断。
- `at()` 返回**新的视图**并共享同一连接与日志，不修改原会话；只想覆盖一次用 `run(cwd=...)`。
- `session.wait(pattern)` 默认从"下一条"开始扫，匹配不到调用之前的历史；覆盖命令执行期间的输出用 `run(...).wait(pattern)`。
- 会话关闭后仍可读取已收集的日志，但会拒绝新的 `run` / `push` / `wait`。
- `rpm.wsl()` 只校验 `wsl.exe` 存在，发行版没装会延迟到首次 `run()` 时以 `CommandError` 报出来。

## 完整规格

[`doc/core-lib-requirements.md`](doc/core-lib-requirements.md)
