# RedPyMake

一个跨运行环境（本地 / SSH / ADB / 串口）的 Python 自动化库：用统一的会话接口
执行命令、传输文件、收集日志、按需做过时判断。

- **单一入口**：`import redpymake as rpm`
- **平台无关的调用**：`session.run(...)` / `session.push(...)` / `session.wait(...)`
- **可选平台依赖**：SSH 与串口按需 `pip install redpymake[ssh,serial]`
- **无 UI 依赖**：核心库不引入 Streamlit / NumPy / SciPy / pydub

## 安装

需要 Python 3.10+。

```bash
pip install -e .          # 仅核心
pip install -e ".[ssh]"   # SSH 支持
pip install -e ".[serial]"  # 串口支持
pip install -e ".[dev]"   # 开发（含测试依赖）
```

## 前十分钟

### 本地命令

```python
import redpymake as rpm

with rpm.local() as here:
    res = here.run("python", "-c", "print('hi')")
    print(res.stdout)
```

### SSH 命令与文件传输

```python
import redpymake as rpm

with rpm.ssh("192.168.1.10", user="root") as remote:
    workspace = remote.at("/workspace")

    # 命令执行
    workspace.run("cmake", "-B", "build")

    # 增量：只有源比目标新才重新构建
    target = workspace.path("build/app")
    sources = [workspace.path("src/main.c"), workspace.path("src/utils.c")]
    if rpm.stale(target, depends_on=sources, name="build_app"):
        workspace.run("cmake", "--build", "build", "-j8")

    # 跨会话推送
    local = rpm.local()
    remote.pull(target, local.path("dist/app"))

    # 保存整个会话的日志
    remote.logs.save("logs/build.log")
```

### 串口日志等待

```python
import re
import redpymake as rpm

with rpm.serial("COM3", baudrate=115200) as uart:
    uart.run("reboot").wait("U-Boot", timeout=10)
    uart.wait(re.compile(r"login:\s*$"), timeout=60)
    uart.run("root").wait("#", timeout=5)
```

## 接口分层

| 层次 | 主要接口 | 说明 |
| --- | --- | --- |
| 一级：核心执行 | `session.at`, `session.run`, `session.path` | 会话、工作目录、命令、路径 |
| 二级 A：文件传输 | `session.push`, `session.pull`, `session.copy` | 方向明确的便利接口，底层统一为 copy |
| 二级 B：日志与等待 | `session.logs`, `session.wait`, `run(...).wait` | 自动收集、订阅、tag、等待 |
| 二级 C：过时判断 | `rpm.stale(target, depends_on=...)` | 增量优化辅助（不是构建系统） |

## 语义要点

- `run()` 默认 `check=True`：非零退出立即抛 `CommandError`；`check=False` 返回 `CommandResult` 让调用者判断。
- 位置参数模式 `run("make", "-j8")` 安全传参、不解析 shell 元字符；需要管道时用 `run(..., shell=True)`（不接受额外位置参数）。
- `at()` 返回**新的视图**并共享同一连接与日志，不修改原会话；单次覆盖用 `run(cwd=...)`。
- `ResourcePath` 绑定所属会话，`session.push(local_path, remote_path)` 无需手工判断平台。
- `rpm.stale(...)` 只返回 `bool`；策略默认 `"mtime"`，依赖不存在始终抛 `InputNotFoundError`。
- `run(...).wait(...)` 使用命令执行前保存的游标搜索，不会漏掉命令期间产生的匹配。
- 会话关闭后仍可读取已收集的日志，但拒绝新的 `run` / `push` / `wait`。

完整需求与验收标准见 [`doc/core-lib-requirements.md`](doc/core-lib-requirements.md)。

## 测试

```bash
pytest              # 默认单元测试，不依赖网络或真实设备
pytest -m integration   # 需要真实 SSH / ADB / 串口设备
```
