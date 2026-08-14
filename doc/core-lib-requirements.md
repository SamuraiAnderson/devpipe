# RedPyMake 核心库重构需求草案

**版本：** v0.5  
**日期：** 2026-08-14  
**范围：** 核心 Python 库（忽略 Web 可视化与运行观测）  
**兼容性：** 不兼容旧 API，允许一次性重构

---

## 1. 定位

RedPyMake 是一个跨运行环境的 Python 自动化库。调用者通过统一的会话接口，在本地、SSH、ADB、串口环境中：

- 执行命令
- 操作与传输文件
- 自动收集会话日志，并等待日志中的特定内容
- 可选：在昂贵操作前做"过时判断"（stale predicate），跳过已完成的任务

调用体验对齐 Python 常用库（`numpy` / `pandas` / `pathlib` / `subprocess`）：

- 单一稳定的顶层入口：`import redpymake as rpm`
- 名称直观、可自动补全
- 参数与返回值语义一致
- 常见操作不需要了解底层平台
- 复杂能力按需展开

---

## 2. 第一阶段范围

### 包含

- 会话连接与工作目录
- 命令执行
- 路径与文件状态
- 跨环境文件传输（二级接口组 A）
- 会话日志自动收集与等待（二级接口组 B）
- 过时判断辅助函数 `rpm.stale(...)`（二级接口组 C）
- 脚本对象与日志分流 `rpm.script(...)`（二级接口组 D）
- 统一异常与结果对象
- 标准 Python 包结构

### 不包含

- Web UI、AST 脚本分析、Web 日志分流
- 全局控制器注册表
- DAG 调度
- 原生异步执行
- 远程后台进程生命周期（spawn/kill）
- TFTP 核心集成（可作为可选 extra）
- 内容哈希增量策略（第一版仅时间戳）
- 连接重试 / 退避

---

## 3. 目标调用方式

```python
import redpymake as rpm

local = rpm.local()

with rpm.ssh("192.168.1.10", user="root") as remote:
    workspace = remote.at("/workspace")

    workspace.run("cmake", "-B", "build")

    target = workspace.path("build/app")
    sources = [workspace.path("src/main.c"), workspace.path("src/utils.c")]

    if rpm.stale(target, depends_on=sources, name="build_app"):
        workspace.run("cmake", "--build", "build", "-j8")

    if rpm.stale(remote.path("/opt/app"), depends_on=target, name="deploy_app"):
        remote.push(target, remote.path("/opt/app"))

    remote.logs.save("logs/build.log")
```

UART 日志等待：

```python
with rpm.serial("COM3", baudrate=115200) as uart:
    uart.run("reboot").wait("U-Boot", timeout=10)
    uart.wait(re.compile(r"login:\s*$"), timeout=60)
```

调用者不应：

- 从深层模块导入（如 `src.linux_controller`）
- 手工判断会话具体类型
- 手工组合 `push` / `pull` 与临时文件
- 解包 `(code, stdout, stderr)` 三元组
- 依赖可变的 `pwd`
- 使用 `target_count=-1` 等魔法参数

---

## 4. 接口分层

### 一级接口：核心执行

```python
session.at(...)
session.run(...)
session.path(...)
```

### 二级接口组 A：文件传输

```python
session.push(...)
session.pull(...)
session.copy(...)
```

职责仅限于文件与路径资源传输。

### 二级接口组 B：日志收集与等待

```python
session.logs
session.wait(...)
session.run(...).wait(...)
```

职责包括自动收集、查询、订阅、保存日志，以及等待字符串或正则。

### 二级接口组 C：过时判断（增量优化辅助）

```python
rpm.stale(target, depends_on=sources) -> bool
```

一个函数式辅助工具，用于在昂贵操作前判断是否可以跳过。**不是构建系统**，不管调度、并发、依赖图；策略可插拔，默认为文件时间戳。

### 二级接口组 D：脚本对象与日志分流

```python
with rpm.script(name="build", dump_on_error="logs/") as run:
    ...
```

一次脚本运行的容器。把用户脚本侧的标准 `logging` 与进程内所有 `Session` 的日志**合流**到同一份缓冲；退出时若捕获到未处理异常，按 `dump_on_error` **自动落盘**（单文件或多文件包）。详见 CORE-09。

四组二级接口相互独立，可分别扩展。

---

## CORE-01：统一会话

### 工厂函数

```python
rpm.local()
rpm.ssh(host, user=..., port=22, password=..., key_filename=...)
rpm.adb(serial=...)
rpm.serial(port, baudrate=115200)
rpm.wsl(distribution=None, user=None)
```

### 类型命名

| 工厂 | 类型 |
|------|------|
| `rpm.local()` | `LocalSession` |
| `rpm.ssh(...)` | `SshSession` |
| `rpm.adb(...)` | `AdbSession` |
| `rpm.serial(...)` | `SerialSession` |
| `rpm.wsl(...)` | `WslSession` |

### 要求

- SSH / ADB / 串口在**构造时立即连接**；连接失败立即抛出 `SessionConnectionError`。
- 所有会话支持上下文管理器（`with`）。
- `close()` 必须可重复调用，并完整释放传输层资源。
- 关闭后继续操作抛出 `SessionClosedError`。
- 不得在构造时隐式注册到全局 UI 注册表。
- 平台不支持的能力抛出 `UnsupportedOperationError`，不得直接暴露 `NotImplementedError`。
- `at()` 创建的视图共享同一连接、同一日志缓冲。

### WSL 说明（"构造时立即连接"的显式例外）

`rpm.wsl(...)` 面向 Windows 上的 Linux 用户态子系统。与 SSH/ADB 不同，它**只在构造时校验 `wsl.exe` 是否可执行**，不做 distro 级探测；`wsl.exe` 缺失时抛 `SessionConnectionError`，其余"发行版未安装 / 冷启动失败"等情形延迟到首次 `run()` 时以 `CommandError` 呈现。这样构造几乎零延迟，语义更贴近 `LocalSession`。

- 路径风格：`posix`。
- 传输：`push` / `pull` 通过 `/mnt/<drive>/…` 中转到 `wsl -e cp`；`copy` 同理。
- 参数：`distribution` → `wsl -d`；`user` → `wsl -u`；`wsl_path` 可覆盖 `wsl.exe` 路径（主要用于测试注入）。
- 由于不探测 `$HOME`，`~` 前缀不做展开，需要时请给绝对路径或用 `default_cwd="/home/xxx"`。

---

## CORE-02：工作目录与命令执行

### 工作目录：`at()`

```python
workspace = remote.at("/workspace")
workspace.run("make")
workspace.run("ctest")

# 可链式
build = remote.at("/workspace").at("build")

# 单次覆盖
workspace.run("ls", cwd="/tmp")
```

要求：

- `at()` 返回绑定新默认工作目录的会话视图，共享原连接与日志。
- 不修改原会话。
- 优先级：`run(cwd=...)` > `at()` 目录 > 会话默认目录。
- 不使用可变的 `session.pwd = ...`。

### 命令执行：`run()`

普通参数模式（默认，安全传参）：

```python
workspace.run("make", "-j8")
workspace.run("python", "build.py", "--name", "hello world")
```

Shell 模式：

```python
workspace.run("make && ctest", shell=True)
workspace.run("cat app.log | grep error", shell=True)
```

签名：

```python
def run(
    self,
    command: str,
    *args: str,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
    encoding: str = "utf-8",
    log_command: bool = True,
) -> CommandResult:
    ...
```

语义：

- 不提供独立 `sh()`。
- `shell=False`：每个位置参数都是独立参数，不解析 `|`、`&&`、`>`。
- `shell=True`：只允许一条完整命令字符串；同时传额外位置参数时抛出 `TypeError`。
- 底层不得使用简单的 `" ".join(args)`，必须做平台正确的参数传递或转义。
- 输出默认统一为 `str`。
- **默认 `check=True`**：非零退出码立即抛出 `CommandError`。
- `check=False` 时无论退出码多少都返回 `CommandResult`，由调用者判断。
- 连接失败、超时等不受 `check=False` 影响，仍抛对应异常。

### 结果对象

```python
@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    session: Session

    @property
    def ok(self) -> bool: ...

    def raise_for_status(self) -> None: ...

    def wait(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 30,
        *,
        channel: str | None = None,
    ) -> LogMatch:
        ...
```

`CommandResult.wait()` 属于二级接口组 B，见下文。

---

## CORE-03：路径与文件对象

通过会话创建：

```python
source = local.path("build/app")
target = remote.path("/opt/app")
```

使用 `ResourcePath` 替代现有 `UFile`。

要求：

- 路径绑定所属会话。
- 相对路径基于创建它的 `at()` 视图解析。
- 创建后不受其他工作目录变化影响。
- 提供类似 `pathlib` 的接口：
  - `exists()`
  - `is_file()`
  - `is_dir()`
  - `stat()`
  - `remove()`
  - `mkdir()`
  - `name`
  - `parent`
- 统一处理 Windows、POSIX 与 `~`。
- 不使用不可见的长期缓存；如使用缓存，必须提供明确的 `refresh()`。

---

## CORE-04：文件传输（二级接口组 A）

### 三个接口

```python
session.push(source, target)
session.pull(source, target)
session.copy(source, target)
```

语义：

| 方法 | 方向 |
|------|------|
| `push()` | 从其他环境传入**当前会话** |
| `pull()` | 从**当前会话**传到其他环境 |
| `copy()` | 统一复制：当前会话内部，或跨会话 |

```python
local_file = local.path("build/app")
remote_file = remote.path("/opt/app")

remote.push(local_file, remote_file)
remote.pull(remote_file, local.path("backup/app"))
remote.copy(remote.path("/opt/app"), remote.path("/tmp/app"))
```

### 要求

- `push()` / `pull()` 是有明确方向的便利接口，底层统一走 `copy()`。
- 调用者不判断会话具体类型。
- 远端到远端如需本地中转，由库自动完成并清理临时文件。
- 调用 `copy()` 的会话必须是源或目标会话之一，不能作为无关的第三方协调者。
- 支持 `overwrite`、超时等选项。
- 返回 `TransferResult`，至少包含：源、目标、是否实际传输、字节数、耗时。

跨远端示例：

```python
linux.copy(
    android.path("/data/app"),
    linux.path("/opt/app"),
)
```

与增量结合（`rpm.stale` 只做判断，执行仍由会话完成）：

```python
if rpm.stale(remote_file, depends_on=local_file):
    remote.push(local_file, remote_file)
```

---

## CORE-05：过时判断 stale predicate（二级接口组 C）

### 定位

"如果目标已经是最新，跳过昂贵操作"是常见到值得封装的模式，但**不是本库的核心业务**。核心业务是跨环境执行、传输、日志；`stale` 只是**增量优化辅助**。

明确不是：

- 构建系统（不管调度、并发、依赖图）
- 事务系统（不保证操作的幂等性，只判断"要不要跳"）
- 完整的缓存层

因此本节以**顶级函数** `rpm.stale(...)` 提供，不引入任何有状态对象（无 `Rule` / `MakeResult` / `MakeContext`）。

### 顶级入口

```python
def stale(
    target: PathSpec | Iterable[PathSpec],
    depends_on: PathSpec | Iterable[PathSpec] = (),
    *,
    strategy: str | StalePredicate = "mtime",
    name: str | None = None,
) -> bool: ...
```

一次调用即用即抛，返回 `True` 表示"目标已过时，需要重做"。

### 典型用法

```python
sources = [workspace.path(p) for p in ("src/main.c", "src/utils.c")]
target = workspace.path("build/app")

if rpm.stale(target, depends_on=sources, name="build_app"):
    workspace.run("cmake", "--build", "build", "-j8")

if rpm.stale(remote.path("/opt/app"), depends_on=target, name="deploy_app"):
    remote.push(target, remote.path("/opt/app"))
```

多目标、多依赖：

```python
if rpm.stale(
    [workspace.path("build/app"), workspace.path("build/app.map")],
    depends_on=[workspace.path("src/main.c"), workspace.path("src/utils.c")],
):
    workspace.run("make", "-j8")
```

### 路径规格

同 CORE-03 / CORE-04：

```python
PathSpec = str | os.PathLike[str] | ResourcePath
```

- `str` 与 `pathlib.Path` 相对**当前进程目录**（本地）。
- 需要相对某会话工作目录时，通过 `session.path(...)` 得到 `ResourcePath`。
- `ResourcePath` 保留自身会话，允许跨设备依赖。
- 单个路径与路径集合都支持。
- 不支持 `open()` 返回的文件流对象。

### 策略可插拔

```python
strategy = "mtime"    # 默认；比较文件修改时间
strategy = "hash"     # 预留；内容哈希；第一版抛 UnsupportedOperationError
strategy = predicate  # 用户自定义谓词
```

自定义谓词签名：

```python
class StalePredicate(Protocol):
    def __call__(
        self,
        targets: Sequence[ResourcePath],
        sources: Sequence[ResourcePath],
    ) -> bool: ...
```

### 判断规则（第一版：`mtime` 策略）

```text
任一依赖不存在                     → 抛出 InputNotFoundError
任一目标不存在                     → True    (target_missing)
最新依赖时间 > 最旧目标时间         → True    (source_newer)
否则                               → False   (up_to_date)
```

限制（需在文档中明确）：

- 目录只比较目录自身的修改时间，不递归比较内容。
- 跨设备比较时间戳要求设备时钟基本同步。
- 内容哈希为后续版本策略，本版不做。
- 依赖不存在**始终抛出 `InputNotFoundError`**，不当作 `True`，避免掩盖依赖被误删的问题。

### 适用与不适用场景

**适用**：

- 编译产物：`build/*` vs `src/*`
- 大文件传输：`remote:/opt/app` vs `local:./dist/app`
- 音频 / 固件生成：产物 vs 输入素材

**不适用**（应使用其它 API）：

- 服务是否已启动 → `session.wait(...)` 匹配日志
- 命令是否成功 → `CommandResult.ok`
- 数据是否已下载完整 → 未来的 `strategy="hash"` 或版本号谓词
- 有副作用且无产物的操作 → 由调用者自己判断

### 与日志的关系

每次 `rpm.stale(...)` 求值写一条 `stale.check` 日志，字段包含：`name`、`strategy`、`result`、`reason`、`targets`、`depends_on`、耗时。

日志写入所属会话缓冲：

- 优先第一个 `ResourcePath` 所属的会话
- 全部为本地路径时写入 local 会话或全局 `redpymake` logger

若需要将跳过或执行分支的后续日志按名称聚合，用通用能力 `session.logs.tag(...)`（见 CORE-06）：

```python
if rpm.stale(target, depends_on=sources, name="build_app"):
    with remote.logs.tag(step="build_app"):
        workspace.run("cmake", "--build", "build", "-j8")
```

### 明确放弃的能力（与 v0.4 差异）

- 抛弃 `rpm.make(...)` 名称，改为 `rpm.stale(...)`（去 Make 化，避免被误当作构建系统）。
- 抛弃 `Rule` 对象：一次判断即用即抛，函数式 API；不引入任何有状态对象。
- 抛弃 `Reason` / `MakeStrategy` 枚举返回：`rpm.stale(...)` 只返回 `bool`；原因写入日志。
- 抛弃"顶级一等公民"地位：从一级接口降级为二级接口组 C（增量优化辅助）。

---

## CORE-06：日志收集与等待（二级接口组 B）

### 自动收集

每个会话创建时自动拥有独立日志缓冲：

```python
with rpm.ssh(...) as remote:
    remote.run("make", "-j8")

records = remote.logs.records()
text = remote.logs.text()
remote.logs.save("logs/build.log")
```

自动收集内容：

- 连接开始、成功、失败、关闭
- 执行的命令与工作目录
- stdout / stderr 流式输出
- 命令退出码与耗时
- push / pull / copy 开始及完成
- `rpm.stale(...)` 求值时的 `stale.check` 事件（strategy、result、reason、targets、depends_on、耗时）
- 超时与异常

结构化记录：

```python
SessionLogRecord(
    timestamp=...,
    sequence=...,
    session_id=...,
    event="command_output",
    level="INFO",
    stream="stdout",   # stdout / stderr / serial / system
    message="...",
    operation_id=...,
    fields={...},
)
```

要求：

- `at()` 视图与原会话共享日志。
- 每个命令有唯一 `operation_id`，关联开始、输出、结束。
- stdout 与 stderr 保留来源标识。
- `check=True` 抛出异常前必须完整记录失败输出。
- 缓冲线程安全，且有容量上限。
- 会话关闭后仍可读取和保存日志。
- 支持订阅实时日志（供未来 UI 使用）：

```python
unsubscribe = remote.logs.subscribe(on_record)
```

- 同时接入标准 `logging`，但库不得调用 `logging.basicConfig()`。
- 密码与环境变量值默认不记录。
- 支持单次命令关闭命令文本记录：`run(..., log_command=False)`。

### 分组标签：`logs.tag()`

用于将一段代码块内的日志打上共同标签（如业务步骤名、`stale.check` 的 `name`），便于后续查询与聚合：

```python
with remote.logs.tag(step="build_app"):
    workspace.run("cmake", "--build", "build", "-j8")
```

要求：

- 标签只作用于 `with` 块内产生的日志记录（写入 `fields`）。
- 支持嵌套；内层标签与外层合并，同名键内层覆盖外层。
- 线程 / 协程安全：不同线程的 `tag()` 互不影响。
- 与 `rpm.stale` 无绑定关系，是通用能力。

### 等待日志

适合 UART 启动日志等数据流场景。

直接等待（默认只匹配调用之后产生的日志）：

```python
match = uart.wait("U-Boot", timeout=30)
match = uart.wait(re.compile(r"login:\s*$"), timeout=60)
```

执行命令后等待：

```python
match = uart.run("reboot").wait("login:", timeout=60)
```

`run(...).wait(...)` 是便利语法，本质等价于：

```python
cursor = session.logs.cursor()
result = session.run(...)
match = session.wait(pattern, since=cursor, timeout=...)
```

时序要求：`run()` 必须在执行前保存日志游标；`.wait()` 先搜索已有日志，再订阅新日志，避免漏掉 `run()` 期间已产生的匹配。

签名：

```python
def wait(
    self,
    pattern: str | re.Pattern[str],
    timeout: float = 30,
    *,
    channel: str | None = None,
    since: LogCursor | None = None,
) -> LogMatch:
    ...
```

匹配规则：

- 普通字符串按字面量匹配。
- `re.Pattern` 按正则匹配。
- `channel` 可限制来源：`"stdout"` / `"stderr"` / `"serial"` / `"system"`。
- 不指定 `channel` 时匹配当前会话的所有数据日志。

返回：

```python
match.pattern
match.record
match.text
match.elapsed
match.command_result   # 由 CommandResult.wait() 触发时有值
```

超时抛出 `LogWaitTimeoutError`，包含 `pattern`、`timeout`、`records`、`output`、`command_result`。

其他约束：

- 多个 `wait()` 可同时等待不同内容。
- 会话关闭时，未完成的等待抛出 `SessionClosedError`。
- `check=True` 的命令失败会先抛 `CommandError`，不会继续 `.wait()`；需要继续等待时使用 `check=False`。
- `rpm.stale(...)` 是纯判断函数，没有链式 `.wait()`；需要在会话侧显式分支：

```python
if rpm.stale(target, depends_on=sources):
    workspace.run("start_service").wait("ready", timeout=30)
else:
    workspace.wait("ready", timeout=30)
```

UART 完整示例：

```python
with rpm.serial("COM3", baudrate=115200) as uart:
    uart.run("reboot").wait("U-Boot", timeout=10)
    uart.wait(re.compile(r"login:\s*$"), timeout=60)
    uart.run("root").wait("#", timeout=5)
```

---

## CORE-07：统一异常

```text
RedPyMakeError
├── SessionError
│   ├── SessionConnectionError
│   └── SessionClosedError
├── CommandError
│   └── CommandTimeoutError
├── TransferError
├── ResourceError
│   ├── ResourceNotFoundError
│   └── InputNotFoundError
├── LogWaitTimeoutError
└── UnsupportedOperationError
```

要求：

- 异常保留可程序化访问的字段：会话、命令、返回码、stdout/stderr、源、目标、超时等。
- 不得混杂抛出 Paramiko 原生异常、普通 `RuntimeError` 与框架异常。
- 所有远程/传输错误继承 `RedPyMakeError`。

---

## CORE-08：Python 库化

- 使用 `src/redpymake/` 标准包结构。
- 使用 `pyproject.toml`。
- 支持 `pip install -e .`。
- 推荐导入：`import redpymake as rpm`。
- 顶层公开 API 由 `redpymake.__all__` 明确定义。
- 公共 API 具有完整类型注解与 docstring。
- 提供 `py.typed`。
- 核心依赖中移除 Streamlit、NumPy、SciPy、pydub。
- SSH、串口、TFTP 可拆为可选 extra。
- 核心库不配置全局日志，只使用标准 `logging`。
- 建议最低 Python 版本为 3.10。
- 不兼容旧的 `LocalHost` / `Linux` / `AdbCnet` / `UFile` / `make_style_decorate` API。

---

## CORE-09：脚本对象与日志分流（二级接口组 D）

### 动机

调用者的日常场景往往是"跑一段脚本，其中可能开多个会话（local + ssh + adb），过程用 `logging` 打业务日志；一旦崩了要能把**全部现场**（业务 logging + 所有 session 日志）一键落盘做事后取证"。CORE-06 只解决了"每个会话独立收日志"，缺三块拼图：

1. **"脚本"作为语义对象**：能持有一次运行的生命周期、注册表、异常边界；
2. **`logging` → session 日志的桥**：把标准 `logging.LogRecord` 归一到与 `SessionLogRecord` 同构的合流缓冲；
3. **异常自动落盘策略**：不由用户在 `try/except` 里手写，而由脚本对象在 `__exit__` 时按配置执行。

CORE-09 只做这三块。**不新增日志类型**——"脚本日志"就是标准 `logging`，不引入 `ScriptLogs`；session 日志仍归 `SessionLogs`（CORE-06）。

### 顶级入口

```python
def script(
    name: str | None = None,
    *,
    dump_on_error: "str | os.PathLike[str] | bool | Callable[[ScriptSnapshot], None] | None" = None,
    log_level: str = "INFO",
    loggers: "Sequence[str] | None" = None,
) -> ScriptRun: ...
```

返回 `ScriptRun`，只能作为上下文管理器使用：

```python
with rpm.script(name="build", dump_on_error="logs/") as run:
    logging.getLogger("myapp").info("start")
    local = rpm.local()                # 自动登记到 run
    remote = rpm.ssh("10.0.0.1")       # 自动登记到 run
    remote.run("make", "-j8")          # 命令日志同时进 remote.logs 与 run
```

- `name`：脚本名；用于落盘目录/文件名与 `meta.json`。缺省用 `"script"`。
- `dump_on_error`：见"落盘策略"。
- `log_level`：`"DEBUG" / "INFO" / "WARNING" / "ERROR"`（大小写不敏感），Handler 上的门槛。
- `loggers`：opt-in 白名单；给出时**只**监听命名列表里的 logger 及其子 logger（`"myapp"` 会覆盖 `"myapp.sub"`），不再挂 root；缺省时挂 root。

### 生命周期

`__enter__`：
1. 设置线程本地 `ContextVar[_current_script]`，指向当前 `ScriptRun`；
2. 装 `_ScriptLoggingHandler`：`loggers` 缺省时装在 root logger，否则装在每个命名 logger 上；handler `level` 按 `log_level`；
3. 记录 `started_at = time.time()`。

`__exit__(exc_type, exc, tb)`：
1. 无条件卸 handler 并 reset ContextVar（幂等）；
2. `detach` 所有已登记 session（取消 `subscribe`）；
3. `ended_at = time.time()`；
4. 若 `exc is not None and dump_on_error not in (None, False)`：调 `_dump(exc, tb)`；
5. **不吞异常**：原异常继续外抛；`_dump` 内部任何失败只走 `_diag_logger.exception(...)`，不掩盖原异常。

### Session 登记（两种方式并存）

**隐式自动**：Session 构造时（仅对 root，`parent is None`）读取 `_current_script`；有活跃 `ScriptRun` 就 `run.attach(self)`。`at()` 视图共享 root 的 `LogBuffer`，无需重复登记。

**显式**：
```python
run.attach(sess) -> None
run.detach(sess) -> None
```

登记按 `session.session_id` 去重，重复 `attach` 无副作用。attach 语义 = `unsub = sess.logs.subscribe(run._on_record)`，即"**拷贝转发**"每条新记录到 `run._merged`；不共享 `LogBuffer`，不破坏 CORE-06 的 per-session 语义。

### `logging` 桥

`_ScriptLoggingHandler` 收到 `LogRecord` 时，转成一条 `SessionLogRecord`：

- `event = "user_log"`
- `stream = "python"`
- `level = record.levelname`
- `message = record.getMessage()`
- `session_id = f"script:{name}"`（区别于任何真实会话）
- `fields = {"logger": record.name, "pathname": record.pathname, "lineno": record.lineno, "funcName": record.funcName}`
- `timestamp = record.created`

塞入 `run._merged`。这条不进任何 `Session` 的 `LogBuffer`（保持"session 侧只装 session 事件"的语义）。

约束：
- 库仍不 `logging.basicConfig()`（CORE-06 约束不变）；Script 只挂 Handler，不改任何 logger 的 `level`。
- 用户仍需自己在脚本侧配 `StreamHandler` 才能在终端看到 logging 输出。CORE-09 只负责"事后落盘"，不主动接管 CLI。

### 快照

```python
run.snapshot() -> ScriptSnapshot
```

```python
@dataclass(frozen=True)
class ScriptSnapshot:
    name: str
    started_at: float
    ended_at: float | None
    records: tuple[SessionLogRecord, ...]  # 按 (timestamp, session_id, sequence) 稳定排序
    sessions: tuple[SessionInfo, ...]      # {id, kind, label}
    exception: ExceptionInfo | None        # {type, message, traceback}
```

- 快照可在 `__exit__` 之前调用（此时 `exception=None`），也可在传给 callable sink 时使用。

### 落盘策略

`dump_on_error` 值分派：

| 值 | 行为 |
|----|------|
| `None` / `False` | 关闭 |
| `Callable[[ScriptSnapshot], None]` | 调 sink 一次，snapshot 已经填了 exception |
| `str | os.PathLike`，`Path(p).suffix != ""` 且不以路径分隔符结尾 | **单文件**（方案 A） |
| 其他 `str | os.PathLike` | **目录包**（方案 C） |

**方案 A：单文件**

写到目标路径。缺失的父目录自动 `makedirs(..., exist_ok=True)`。内容 = 排序后 `_merged` 逐行 `"{ISO time} [{level}] [{session_id}] {event}: {message}"`；末尾追加异常摘要（type + message + traceback）。

**方案 C：目录包**

在 `dump_on_error` 指定目录下创建子目录 `<name>-<YYYYmmddTHHMMSS>/`，写入：

- `all.log`：同 A 格式的全量流。
- `script.log`：仅 `event == "user_log"` 的记录，用 logging 的可读格式。
- `<session_id_safe>.log`：每个已登记 session 一份，含该 session 的全部记录（`session_id_safe` = 把 `:`、`/`、`\`、空白替换为 `_`）。
- `meta.json`：
  ```json
  {
    "name": "build",
    "started_at": 1234567890.12,
    "ended_at": 1234567895.67,
    "exception": {"type": "CommandError", "message": "...", "traceback": "..."},
    "sessions": [{"id": "local:local#1", "kind": "local", "label": "local"}]
  }
  ```

### 嵌套

`ContextVar` 天然按线程/协程隔离。嵌套 `rpm.script()` 时：内层 `ScriptRun` 是独立作用域，内层 Session 只登记到内层；内层退出后 `ContextVar` 恢复为外层引用，外层继续收集。

### 与 CORE-06 的关系

- CORE-06 不变：`sess.logs.records / text / save / subscribe / tag / wait` 语义、`LogBuffer` 容量与线程模型、`operation_id` 关联全部保持。
- CORE-09 是**"面向脚本"的合流层**，构建于 `SessionLogs.subscribe` 之上；不改 CORE-06 任何 API。

### 明确不做的能力

- 不做默认 CLI sink（不主动把日志打到 stderr）；用户仍自行配 StreamHandler。
- 不做流式落盘（append-mode file handle）；落盘只在异常触发时一次性写出。
- 不做每条 `subscribe` 的用户 API 装饰（想过滤请自己在 sink 里做）。
- `run.snapshot()` 只读；不提供 `run.records()` / `run.text()` 等被动查询的完整 API 面（避免与 `SessionLogs` 重复）。

---

## 5. 验收标准

1. README 前十分钟示例可以完成本地命令、SSH 命令和文件复制。
2. 调用者无需 `sys.path.insert()`。
3. 核心操作无需判断具体会话类型。
4. 所有 `run()` 返回相同类型 `CommandResult`。
5. 文件传输通过 `push` / `pull` / `copy` 完成，调用者不写平台分支。
6. 过时判断使用顶级函数 `rpm.stale(...) -> bool`；不再出现 `target_count`、`Rule`、`.run()` / `.call()` 链式。
7. 默认单元测试不需要网络或真实设备。
8. SSH、ADB、串口测试标记为集成测试。
9. 核心包测试覆盖率建议不低于 80%。
10. UI 完全移除后，核心库仍可独立安装和运行。
11. `run(...).wait(...)` 不会漏掉命令执行期间已产生的日志。
12. 会话关闭后仍可读取已收集的日志。
13. `with rpm.script(dump_on_error=...)`：块内未处理异常时按配置自动落盘（含标准 `logging` 与所有登记 session 的日志）；正常退出不落盘；落盘失败不掩盖原异常。

---

## 6. 已确认决策

| 项 | 决策 |
|----|------|
| `run()` 默认 `check` | `True` |
| 远端会话连接时机 | 构造时立即连接 |
| 旧 API 兼容 | 不兼容，一次性重构 |
| 增量第一版策略 | 仅时间戳（mtime） |
| 工作目录 | `at()` 视图，不用可变 `pwd` |
| Shell 语法 | `run(..., shell=True)`，不单独提供 `sh()` |
| 命令参数形式 | `run("make", "-j8")`，不用列表括号 |
| 增量 API | 二级接口组 C：顶级函数 `rpm.stale(target, depends_on=..., strategy=..., name=...) -> bool` |
| 增量 API 的地位 | 增量优化辅助，不是核心业务；不是构建系统 |
| 是否引入 `Rule` 对象 | 不引入；一次判断即用即抛，无状态函数式 API |
| 增量策略 | 默认 `"mtime"`；预留 `"hash"` 与自定义谓词接口 |
| 路径类型 | `str` / `os.PathLike` / `ResourcePath`（推荐 `ResourcePath`，跨会话依赖天然支持） |
| 文件传输 | 二级接口组 A：`push` / `pull` / `copy` |
| 日志与等待 | 二级接口组 B：`logs` / `wait` / `run().wait` |
| 日志分组标签 | `session.logs.tag(...)`，与 `rpm.stale` 解耦的通用能力 |
| 脚本对象 | 二级接口组 D：`rpm.script(name=..., dump_on_error=..., log_level=..., loggers=...)`，`ScriptRun` 只做容器 + 桥 + 异常边界，不新增日志类型 |
| Session 登记方式 | 隐式（进入 `with rpm.script()` 后 root Session 构造时读 `ContextVar` 自动 attach）+ 显式（`run.attach(sess)`）并存 |
| `logging` 捕获范围 | 默认 root logger + `log_level` 门槛；`loggers=[...]` 时改为 opt-in 白名单 |
| 异常落盘触发 | 仅在 `__exit__` 收到未处理异常时；`dump_on_error` 按值类型分派：单文件 / 目录包 / callable / 关闭 |

---

## 7. 后续版本（非本草案范围）

- 内容哈希增量策略
- 连接重试与退避
- DAG 编排与原生 async
- 远程进程 spawn / wait / kill
- TFTP 作为 optional extra
- Web UI 基于新会话日志订阅接口重建
