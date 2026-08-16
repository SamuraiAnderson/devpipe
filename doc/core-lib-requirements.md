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
- 脚本发现与实时日志（二级接口组 E，CORE-10）
- Workspace 会话池 + 可视化 Web UI（二级接口组 F，CORE-11）
- 统一异常与结果对象
- 标准 Python 包结构

### 不包含

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

### 二级接口组 E：脚本发现与实时日志

```python
cards = rpm.discover("./scripts")           # 列出目录下所有 rpm_*.py 的元数据
os.environ["REDPYMAKE_LIVE_SINK"] = "file:///tmp/live.ndjson"  # 激活 sink
```

**文件级发现**：递归扫 `rpm_*.py`，AST 抽取 `with rpm.script(...)` 元信息；**运行时实时流**：通过环境变量激活 NDJSON 追加写，供外部工具（Web UI、CI）tail 拿到 `SessionLogRecord` 事件流。详见 CORE-10。

### 二级接口组 F：Workspace 与 Web UI

```python
with rpm.workspace("./scripts") as ws:
    ws.enqueue("rpm_hello.py")   # 串行队列：会话跨脚本复用、UI 单一活跃 run
```

`Workspace` 是"一次可视化 / 交互会话生命周期"内的运行时容器：会话池懒创建 + 跨脚本复用、`importlib.reload` 加载脚本、串行队列执行、`ScriptSnapshot` 历史归档。配合 `redpymake serve` 提供纯 Web UI（无桌面 App、无 TUI）。详见 CORE-11。

六组二级接口相互独立，可分别扩展。

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

## CORE-10：脚本发现与实时日志（二级接口组 E）

### 动机

要让"目录里的一堆 `.py` 脚本"能被外部工具（Web UI / CI / 编辑器插件）**发现**和**跟随观察**，需要两块拼图：

1. **静态发现**：不 import、不执行任何脚本，仅通过文件名前缀 + AST 分析，就能列出所有可运行的 rpm 脚本与它们的元数据（名字、docstring、用到哪些 factory）。
2. **实时流**：脚本运行时，把每条 `SessionLogRecord` 同步落到一份 NDJSON 文件，供外部进程按行 tail。

CORE-10 只做这两块。**不引入新的日志类型**——落盘的还是 CORE-06 的 `SessionLogRecord`；**不引入新的入场句**——脚本身份仍是 `with rpm.script(...)`（CORE-09）。前缀与 sink 都是 CORE-09 之上的"取用便利"，不改任何既有 API。

### 文件级发现

**约定**：目录下**文件名匹配 `rpm_*.py`** 的 `.py` 文件被视为可发现的 rpm 脚本。可通过 `pyproject.toml` 覆盖：

```toml
[tool.redpymake.discovery]
patterns = ["rpm_*.py"]                             # 默认唯一值
exclude  = [".git", "__pycache__", ".venv",
            "node_modules", "dist", "build"]
```

以 `_` 开头的 `.py` 文件（如 `_helpers.py`）在任何情况下都不发现，用作"库内部"约定。

### 顶级入口

```python
def discover(
    root: str | os.PathLike = ".",
    *,
    patterns: Sequence[str] | None = None,
    exclude:  Sequence[str] | None = None,
) -> list[ScriptCard]: ...

@dataclass(frozen=True)
class ScriptCard:
    path: Path
    module_name: str
    script_name: str | None       # AST 抽取的 rpm.script(name=...) 字面量
    docstring: str | None         # 模块首个 Expr 字符串
    factories: tuple[str, ...]    # 静态可推：("local", "wsl", ...)
    has_script_block: bool        # 是否发现 with rpm.script(...)
    lineno: int | None            # 上述 with 块的行号
    error: str | None = None      # 语法错误等降级信息；正常时为 None
```

要求：

- **纯 AST 分析**，绝不 `import` 目标脚本，不执行其顶层代码；
- 语法错误的文件不使整个 `discover(...)` 崩溃，返回时 `has_script_block=False` 且 `error` 非空；
- `script_name` 仅当 `rpm.script(name="...")` 是字面量字符串时取得；变量表达式则为 `None`；
- `factories` 静态识别 `redpymake.<X>(...)` 或 `rpm.<X>(...)` 调用，`<X>` 属于 `{"local","ssh","adb","serial","wsl"}` 集合；
- `patterns=None` 时优先读取 `pyproject.toml`（由 `root` 向上找），否则退化为 `("rpm_*.py",)`；
- 文件顺序按相对 `root` 的字典序稳定输出。

### 运行时实时日志（NDJSON sink）

**激活方式**：`ScriptRun.__enter__` 时读取环境变量 `REDPYMAKE_LIVE_SINK`：

- `file:///abs/path/to/live.ndjson`：追加写；父目录不存在自动 `makedirs`；
- 缺省或空字符串：不激活。

**行格式**：每行一个 JSON 对象，字段与 `SessionLogRecord` 对齐：

```json
{"timestamp": 1723691234.5, "sequence": 42, "session_id": "wsl:default#1",
 "event": "command_output", "level": "INFO", "stream": "stdout",
 "message": "hello", "operation_id": "cmd-abc", "fields": {"phase": "smoke"}}
```

**元行**：`ScriptRun.__enter__` / `__exit__` 分别写一条元行，供 tail 端知道边界：

```json
{"timestamp": ..., "event": "script.begin", "name": "hello",
 "pid": 12345, "started_at": ...}
{"timestamp": ..., "event": "script.end",   "name": "hello",
 "ended_at": ..., "exception": {"type": "...", "message": "...", "traceback": "..."} | null}
```

**按 session 分文件（fan-out）**：主文件之外，sink 把每条记录**同时镜像**一份到 `<sink 所在目录>/sessions/` 下的按会话分文件：

```text
<sink 所在目录>/
  live.ndjson                        # 主文件：全量顺序流，权威
  sessions/
    wsl_default_1-3f2a1c9d.ndjson    # 每个 session_id 一份
    __script__.ndjson                # 无 session_id 的行
```

- 主文件语义**不变**：仍是全量顺序流；run 切片（CORE-11 的字节偏移）、`report`、回放一律以它为准。分文件是纯旁路镜像，行内容与主文件里的对应行逐字符相同；
- 文件名由 `session_id` 派生：保留 `[A-Za-z0-9._-]`，其余字符（`:` `#` `@` 路径分隔符等）替换为 `_`；一旦发生替换、超长（> 64 字符）或撞上保留名 `__script__`，追加 `-<sha1(session_id)[:8]>` 后缀，保证不同 id 不会共用一份文件；
- 没有 `session_id` 的行落 `__script__.ndjson`：`script.begin` / `script.end`，以及 CORE-11 里 Workspace 在 sink 外侧追写的 `workspace.run.begin` / `workspace.run.end`。它与 Web 分栏的 Script 兜底列同义；
- 分文件写入失败只走 `_diag_logger.exception(...)`，**不得影响主文件**；分文件句柄随 sink 一起 `close()`；
- 分文件**只写不读**：库自身不从它们重建任何状态，删掉不影响任何功能。

**语义要点**：

- sink 与 CORE-09 的 `_dump_on_error` 落盘策略**正交**——sink 是每行流式追加，`dump_on_error` 是异常时一次性快照；两者可同时开启；
- sink 通过 `SessionLogs.subscribe(...)` 注册，`ScriptRun.__exit__` 时自动 `unsubscribe` 并 close 文件；
- 写入失败（磁盘满 / 权限）只走 `_diag_logger.exception(...)`，不掩盖用户代码异常；
- 库仍不 `logging.basicConfig()`（保持 CORE-06 / CORE-09 约束）。

### CLI

新增 `redpymake` 入口点（`pyproject.toml [project.scripts]`）：

```bash
redpymake discover [ROOT] [--json]           # 列所有 ScriptCard
redpymake run PATH [--sink FILE]             # 独立子进程跑单个脚本 + 挂 sink
redpymake report NDJSON -o report.html       # 从 NDJSON 生成自包含 HTML
redpymake serve [ROOT] [--host H --port P] [--no-open] [--resume-log]   # 见 CORE-11
```

- `discover` 默认输出人类可读树；`--json` 走结构化输出，供机器消费；
- `run` 用 `subprocess.run([sys.executable, path])` 起子进程，环境变量 `REDPYMAKE_LIVE_SINK=file://<path>` 由 CLI 注入；透传脚本 exit code 作为自身 exit code；
- `report` 是纯离线操作：读 NDJSON，输出**自包含**的 HTML（无外链、无网络请求），供 CI artifact / 邮件附件分享。

### 明确不做的能力

- 不支持 `stdout://` / `tcp://` 等 sink URI（第一版仅 `file://`）；
- 不做流式压缩、rotate、文件锁；
- 不做非 Python 脚本发现（`.sh` / `.ps1` 等）。

---

## CORE-11：Workspace 与 Web UI（二级接口组 F）

### 动机

`redpymake serve` 场景下，用户希望在同一个进程里**反复运行多个脚本**并共享会话（WSL 连接、SSH 连接、ADB 设备句柄），避免每次都重连。同时 UI 需要一个稳定的**状态所有者**：脚本清单、会话池、运行队列、当前活跃 run。

`Workspace` 就是这个所有者。它与 `ScriptRun` **正交**：一个 workspace 可跑多个脚本，每个脚本内部依旧用 `with rpm.script(...)` 拿到自己的 `ScriptRun`。

### 顶级入口

```python
def workspace(
    root: str | os.PathLike = ".",
    *,
    logs_root: str | os.PathLike | None = None,      # 默认 <root>/.redpymake/logs/
    ndjson_dir: str | os.PathLike | None = None,     # 兼容旧参数名；等价于 logs_root
    discovery_patterns: Sequence[str] | None = None,
    auto_close_sessions: bool = True,
    log_name: str | None = None,                     # 新建活跃日志时的显示名；默认时间戳
) -> Workspace: ...
```

`Workspace` 只能作为上下文管理器使用：`__exit__` 时把内部起过的所有会话按 `auto_close_sessions` 关掉。

### 会话池

`Workspace` 暴露与 `rpm.*` 工厂同名的方法，返回**共享借出**的会话：

```python
ws.local(**kw)  ws.wsl(distribution=None, **kw)
ws.ssh(host, **kw)  ws.adb(serial=None, **kw)  ws.serial(port, **kw)
ws.sessions() -> Mapping[str, Session]   # 只读快照
```

按 key 缓存与复用：

| 工厂 | key |
|------|-----|
| `local()` | `"local"` |
| `wsl(distribution)` | `f"wsl:{distribution or 'default'}"` |
| `ssh(host, port, user)` | `f"ssh:{user}@{host}:{port}"` |
| `adb(serial)` | `f"adb:{serial or 'default'}"` |
| `serial(port, baudrate)` | `f"serial:{port}@{baudrate}"` |

**同 key 冲突参数**（例如同一 wsl distro 但不同 `user`）抛 `ValueError("workspace session key conflict: ...")`，逼调用方显式区分。

### ContextVar 联动：脚本代码零改动

Workspace 通过 `contextvars.ContextVar[_active_workspace]` 让顶层工厂**在 workspace 作用域内自动借出**：

```python
# _factory.py（简化）
def wsl(distribution=None, **kw):
    ws = _active_workspace.get()
    if ws is not None:
        return ws.wsl(distribution, **kw)    # 借出共享会话（_BorrowedSession 代理）
    return WslSession(distribution, **kw)    # 独立会话（CLI 直跑等价）
```

借出的代理会话：

- `__enter__` 返回真会话；
- `__exit__` **不 close** 真会话（Workspace 拥有生命周期），只做 ScriptRun 记账；
- 其它属性/方法完全透传给真会话。

**效果**：同一个 `rpm_hello.py`，`python rpm_hello.py` 直跑时创建独立会话并自动关，Workspace 里跑时借出共享会话并延续到下一个脚本。脚本源码不需要感知 workspace。

### 脚本执行

```python
ws.enqueue(path) -> str                    # 入队，返回 run_id；不阻塞
ws.stop_current() -> None
ws.pause_queue()  ws.resume_queue()  ws.clear_queue()
ws.current_run -> WorkspaceRun | None
ws.runs -> Sequence[WorkspaceRun]          # queued + running + done，时间倒序
ws.get_run(run_id) -> WorkspaceRun
ws.iter_run_records(run_id) -> Iterator[dict]  # 读该 run 的 NDJSON
ws.discover() -> list[ScriptCard]          # 委托到 CORE-10
ws.refresh() -> None                       # 重扫脚本
```

**执行模型**：

- 内部单线程 FIFO 队列 + 一个工作线程；**同一时刻至多一个脚本在跑**；
- 用户随时可 `enqueue`（含正在跑的时候）；新增项追加到尾部；
- 工作线程加载脚本：`importlib.util.spec_from_file_location(...)` + `spec.loader.exec_module(...)`，模块若已在 `sys.modules` 里则 `importlib.reload(module)` 刷新代码（**每次 ▶ 自动 reload**）；
- 加载后要求脚本暴露可调用的 `main`；缺失时 `WorkspaceRun.status="failed"`, `exception="entry_missing"`；
- 在 `_active_workspace` 已设定的作用域里调 `module.main()`；捕获全部异常写进 `WorkspaceRun.exception`，不让主进程挂；
- 每次运行前设置 `REDPYMAKE_LIVE_SINK=file://<ndjson_path>`，由 CORE-10 的 sink 落盘；
- 运行结束把 `ScriptRun.snapshot()` 写进 `WorkspaceRun.snapshot`。

### `WorkspaceRun` 数据模型

```python
@dataclass(frozen=True)
class WorkspaceRun:
    id: str                          # 如 "hello#3"
    script_path: Path
    script_name: str                 # 从 ScriptCard 或 stem 取
    status: str                      # "queued" | "running" | "succeeded" | "failed" | "cancelled"
    started_at: float | None
    ended_at:   float | None
    exception:  str | None           # 简短错误概述；完整 traceback 走 NDJSON script.end 元行
    ndjson_path: Path
    snapshot: ScriptSnapshot | None
```

### 生命周期

`Workspace.__enter__`：

1. 校验 `root` 存在；
2. 建 NDJSON 目录（默认 `<root>/.redpymake/runs/`），确保可写；
3. 启动内部工作线程（daemon）；
4. 返回 self。

`Workspace.__exit__`：

1. 停接受新 `enqueue`（后续调用抛 `RuntimeError("workspace is closed")`）；
2. 若当前 run 仍在跑：向工作线程发终止请求；`stop_current` 等待其收尾；
3. `auto_close_sessions=True`（默认）时关掉会话池中所有会话，捕获异常写诊断日志、不抛；
4. 工作线程 join。

### 事件订阅（供 UI 消费）

`ws.subscribe(callback) -> Callable[[], None]`：注册回调，Workspace 状态变化时被调（会话新增/关闭、脚本队列变化、run 状态迁移、当前 run 增量记录、日志组变更）。事件对象是纯 dict，跨线程安全。Web UI 内部使用；不属于稳定用户 API。

事件类型：

| type | payload | 时机 |
|------|---------|------|
| `run.enqueued` | `{run_id, log_id}` | `enqueue` 成功 |
| `run.started` | `{run_id}` | worker 开始跑 |
| `run.finished` | `{run_id, status}` | worker 跑完（含失败 / 被 stop） |
| `run.record` | `{run_id, record}` | 脚本内产生一条 `SessionLogRecord`（payload 恒为 record 形状） |
| `run.meta` | `{run_id, record}` | 写下一条 `workspace.run.begin` / `workspace.run.end` 边界元行 |
| `run.cancelled` | `{run_id}` | `cancel_run` 把一个 queued run 剔出队列 |
| `run.stopping` | `{run_id}` | `stop_current` 收到停止请求（run 尚未收尾） |
| `log.rotated` | `{log_id, previous_log_id}` | `rotate_log` 成功 |
| `log.renamed` | `{log_id, name}` | `rename_log` 成功 |
| `log.pinned` | `{log_id, pinned}` | `pin_log` 成功 |
| `log.discarded` | `{log_id}` | `discard_log` 成功 |

### 日志组（Workspace log groups）

**动机**：一次 `serve` 交互期间会产生若干 run（脚本反复调），用户希望能**按主题分组**（"api 重构那一批"、"scratch 试验"、"CI-2026-08-15"）+ **跨 `serve` 重启保留**。

**模型**：一个 workspace root 下可以有多份**命名日志**（`WorkspaceLog`），任意时刻恰好一份是**活跃**的。

**关键语义：一份 workspace 日志 = 一条连续的 `stream.ndjson` 流。** Web UI 里跑的所有 run 的记录**都追加进同一个文件**——不做 per-run / per-script 分片。一份日志就是一次"录制会话"，里面的 run 只是流上的片段。用户可 rename / rotate（清空 = 归档旧的、起新的）/ pin / discard。

这个决策带来的直接收益：一份日志 = 一个自包含 NDJSON 文件，可以直接喂给 `redpymake report`，导出 / 分享 / 归档零打包成本。

**启动语义**：`Workspace(new_log_on_start=True)` 进入时把恢复出来的活跃日志降级为历史、另起一份新的活跃日志；若那份**没有 run（`run_count == 0`）则原地复用**，免得反复重启堆一串空日志。`redpymake serve` 默认走这条路——一次 `serve` 就是一次独立的录制会话，`--resume-log` 可续用上次那份。库内默认 `False`：REPL / 脚本里的 `rpm.workspace(root)` 仍然续用上次活跃日志。

```python
@dataclass(frozen=True)
class WorkspaceLog:
    id: str              # 目录名：<YYYY-MM-DDTHHMMSS>-<6-hex>
    name: str            # 用户可编辑显示名；默认 = id 的时间戳部分
    created_at: float
    description: str
    pinned: bool         # True 时 discard_log 拒绝硬删
    root: Path           # <logs_root>/<id>/
    run_count: int
    is_active: bool
```

**API**：

```python
ws.current_log -> WorkspaceLog                       # 活跃日志（写入目标）
ws.list_logs() -> list[WorkspaceLog]                 # 按 created_at 降序
ws.get_log(log_id) -> WorkspaceLog
ws.rename_log(log_id, name, description=None) -> WorkspaceLog
ws.rotate_log(name=None) -> WorkspaceLog             # 归档当前 + 新建 + 变活跃；run in progress 时拒绝
ws.pin_log(log_id, pinned=True) -> WorkspaceLog
ws.discard_log(log_id) -> None                       # 硬删；活跃或 pinned 时拒绝
ws.list_runs_in_log(log_id) -> list[WorkspaceRun]    # 惰性扫 stream.ndjson 重建
```

**磁盘布局**：

```text
<logs_root>/                                  # 默认 <root>/.redpymake/logs/
  _active.json                                # {"log_id": "..."}；缺失/损坏则回退到最新的 log 或新建
  <log_id>/
    meta.json                                 # {id,name,created_at,description,pinned}
    stream.ndjson                             # 唯一日志流；该 log 下所有 run 的记录顺序追加
    history.json                              # 命令历史：{version, log_id, sessions: {session_id: [{command,timestamp,exit_code,duration}]}}
    sessions/                                 # 按 session 分文件的旁路镜像（见 CORE-10 fan-out）
      <slug>.ndjson                           # 每个 session_id 一份
      __script__.ndjson                       # 无 session_id 的行：script.begin/end + workspace.run.begin/end
```

**没有** `runs.index.jsonl`，**没有** `runs/` 子目录——run 摘要不单独存，一律从 `stream.ndjson` 的元行重建。`sessions/` 同理只写不读：它是给人和外部工具按会话 tail / grep 用的，库自身的重建路径一律只认 `stream.ndjson`。

### `stream.ndjson` 的结构

一份 stream 是若干个 run 段首尾相接。每个 run 段由 Workspace 自己写的一对元行界定，中间夹着 CORE-10 sink 落的内容（`script.begin` / `SessionLogRecord` × N / `script.end`）：

```text
{"event":"workspace.run.begin","run_id":"hello#1","script_path":"...","script_name":"hello","log_id":"...","started_at":...,"timestamp":...}
{"event":"script.begin","name":"hello","pid":1234,"started_at":...}      <- CORE-10 sink
{"event":"command","session_id":"local-1","message":"hi",...}            <- SessionLogRecord
...
{"event":"script.end","name":"hello","ended_at":...,"exception":null}    <- CORE-10 sink
{"event":"workspace.run.end","run_id":"hello#1","status":"succeeded","ended_at":...,"exception":null,"timestamp":...}
{"event":"workspace.run.begin","run_id":"build#2",...}                   <- 下一个 run 紧接着
...
```

**分工**：

- CORE-10 sink（`_live_sink.py`）负责 `script.begin` / `script.end` + 每条 `SessionLogRecord`；它的目标文件是该 log 的 `stream.ndjson`（`REDPYMAKE_LIVE_SINK=file://<log>/stream.ndjson`，sink 本来就是 append 模式），同时按 CORE-10 的 fan-out 规则镜像到 `<log>/sessions/`；
- Workspace 负责在 sink 外侧追写 `workspace.run.begin` / `workspace.run.end`——这两条携带**只有 workspace 知道的元数据**（`run_id` / `script_path` / `status` / `exception`），不污染 CORE-10 的语义；这两条无 `session_id`，除主流外同样镜像进 `<log>/sessions/__script__.ndjson`。

**元行字段**：

| event | 字段 |
|-------|------|
| `workspace.run.begin` | `run_id`, `script_path`, `script_name`, `log_id`, `started_at`, `timestamp` |
| `workspace.run.end` | `run_id`, `status`, `ended_at`, `exception`, `timestamp` |

**Rotate 语义**：

- 若 `ws.current_run is not None`（有 run 在跑或队列非空且 worker 正处理中），`rotate_log` 抛 `RuntimeError("cannot rotate: a run is in progress")`；调用方（Web UI）应先提示用户等或终止；
- 归档 = 停止对旧 log 的 `stream.ndjson` 写入（不物理删除）；
- 新 log 直接成为活跃，`_active.json` 原子替换（tmp + rename）；
- `ws.runs` 跟随活跃日志；历史日志的 runs 走 `list_runs_in_log(log_id)`。

**跨实例恢复**：

- `Workspace.__enter__`：扫 `<logs_root>/*/meta.json` 重建 `_logs`；读 `_active.json`；**顺序扫活跃日志的 `stream.ndjson`**，按 `workspace.run.begin` / `workspace.run.end` 配对重建 `_runs`（历史日志按需惰性扫）；
- 若 `_active.json` 缺失或指向不存在的 log：优先选最新的历史 log 复活，都没有再新建（默认名 = 时间戳）；
- **孤立 `workspace.run.begin`**（有 begin 无 end，即进程崩溃 / 断电 / 强杀留下的残段）→ 重建为 `status="interrupted"`；
- `run_count` 从 stream 里 `workspace.run.begin` 的条数得出。

**`WorkspaceRun` 字段变化**：

- 新增 `log_id: str`（该 run 所属日志的 id）；
- 新增 `stream_offset_begin: int | None` / `stream_offset_end: int | None`（该 run 段在 stream 里的字节区间，回放时可直接 seek，省掉全文件扫描）；
- `ndjson_path` **保留**但语义改为"该 run 所在 log 的 `stream.ndjson`"（向后兼容旧调用方；要单 run 的记录请用 `iter_run_records`）。

**`iter_run_records(run_id)` 的实现**：打开该 run 所属 log 的 `stream.ndjson`，若有 offset 区间则 seek，否则顺序扫；从 `workspace.run.begin(run_id)` 开始 yield，到匹配的 `workspace.run.end(run_id)` 停止。两条 workspace 元行本身也 yield（前端 timeline 需要它们做 run 分隔）。

**新增队列指令**：

```python
ws.cancel_run(run_id) -> bool      # 只对 queued 有效；剔出队列 + 标 cancelled；不落盘 stream
ws.rerun(run_id) -> str            # 等价 enqueue(get_run(run_id).script_path)，返回新 run_id
ws.stop_current() -> bool          # 见下
```

**`stop_current` 语义（尽力而为，不强杀）**：

- 设 `_stop_requested = True` 并广播 `run.stopping` 事件；
- 借出会话（`_BorrowedSession`）在每次 `run()` / `wait()` **进入前**检查该标志，已置起则抛 `WorkspaceStoppedError`——脚本因此在**下一条命令的边界**终止；
- **当前正在执行的子进程不强杀**：一条已经跑起来的长命令会跑完；
- run 收尾时若标志被置起过，`status` 覆盖为 `"cancelled"`（即便脚本自己正常返回）；
- 标志在每个 run 开始前重置。

**`redpymake run`（CLI 独立子进程）不参与本机制**：它继续落独立的 per-script NDJSON 到 `<script_dir>/.redpymake/runs/`，与 workspace 日志目录树完全隔离。

**CLI**：

```powershell
redpymake logs list [ROOT]
redpymake logs rename [ROOT] LOG_ID NEW-NAME [--description "..."]
redpymake logs rotate [ROOT] [--name X]
redpymake logs pin [ROOT] LOG_ID [--no-pin]
redpymake logs discard [ROOT] LOG_ID
```

**Web UI**：

- 顶栏放**日志切换器**：`Log: <当前名 (N runs)> ▾` + `✎ rename`（inline 编辑）+ `⟳ New log`（= rotate）+ `★ Pin` + `🗑 Discard`；
- 切换器下拉列出所有历史日志（按时间倒序，★ pinned 靠前）；点击历史项切换 sidebar Runs 的展示范围；
- Rotate 遇到 running run 时前端弹提示："当前有 run 在跑，请等它跑完（或停掉队列 pause_queue）后再新建日志"，本地不重试。

### Web UI 交互模型（主区两态 + Sidebar 行内操作）

**主区两态**：

| 态 | 进入方式 | 数据源 | 控件 |
|----|---------|--------|------|
| **Live tail**（默认） | 初始态；点 `⇥ Back to Live` | `state.liveBuffer`——挂钩**活跃日志**跨 run 持续累积的 WS 流 | 只有滚动跟随；无 playback |
| **Run detail**（回放） | 点 Sidebar Runs 里任一条目 | 活跃 log 的 run 从 `liveBuffer` 按 `run_id` 过滤；历史 log 的 run 走 `GET /api/logs/{lid}/runs/{rid}/records` | 完整 playback：`▶/⏸` `⏮` `Step` `Speed` `scrubber` |

**关键约束（用户明确要求）**：`state.liveBuffer` 挂钩**活跃日志**而非某个 run——切去看历史 run **不打断** live 累积，随时 `⇥ Back to Live` 都能看到最新尾部，中途新到的记录一条不丢。切换活跃日志（rotate / 选历史 log）时才重置 buffer。

Live tail 里不同 run 之间画分隔条（`─── hello#3 · succeeded (2.1s) ───`），由 `workspace.run.begin/end` 元行驱动。

**Sidebar 行内操作**：

- **不做** Script 详情卡——点脚本名不弹面板；
- Scripts 每条右侧一个 `▶`（悬停显现）：点它才 enqueue，点条目本身无副作用（防误触，同时省掉二次点击）；
- Runs 每条右侧一个**按状态自动切换**的操作按钮：

| run status | 按钮 | 动作 | 路由 |
|------------|------|------|------|
| `queued` | `⏹` | 剔出队列 | `DELETE /api/runs/{rid}` |
| `running` | `⏹` | 请求停止；按下后按钮变灰 + 文案 `stopping…`，等 `run.finished` 事件到达才复位 | `POST /api/runs/{rid}/stop` |
| `succeeded` / `failed` / `cancelled` / `interrupted` | `⟳` | 用同一脚本路径重跑（**不弹确认**） | `POST /api/runs/{rid}/rerun` |

- 主区**只做展示不做操作**——所有运行控制都收敛在 Sidebar 行内；
- Sidebar Runs 里"当前活跃 run"**不自动抢占**主区展示（用户不点就不切）。

**按 session 分栏**（Live 与 Run detail 两态都支持）：

- Sidebar 的 Sessions 段头有 SplitToggle；开启后每条 SessionItem 前出现 SessionCheck，勾中的 session 各占一列；一条都没勾时退回单列；
- 列定义由 PaneSet 持有，**时钟与 PlaybackToolbar 始终只有一份**——Run detail 分栏回放时多列共享同一个播放头，不做多时钟同步；
- 记录按 `session_id` 路由；脚本自身的 `user_log`（合成 id `script:<name>#N`）与 RunSeparator 固定进 ScriptPane；
- 运行中冒出的新会话自动补一列，避免悄悄落进 ScriptPane；SessionList 同时列出"池里的会话"与"当前主区数据里出现过的会话"（后者用 `○` 区分），所以看历史 run 时也能勾选分栏；
- 选择持久化在 `localStorage["rpm.split"]`，刷新页面后保持。

### Web UI DOM 部件命名（交流约定）

页面部件有稳定名字，讨论 / issue / commit message 一律用这套词，避免"左边那个列表"式的指代。名字与 DOM 锚点一一对应，树形结构如下：

- **AppShell** (`body.app-shell`) — 视口锁定外壳：TopBar + Layout 两段
  - **TopBar** (`header`) — 标题与全局日志控制，不参与滚动
    - **BrandBlock** (`.header-left`) — 标题 + 一句话说明
    - **LogSwitcher** (`#log-switcher`) — 当前日志组 + 切换/管理入口
      - **LogCurrentButton** (`#log-current-btn`) — 显示当前日志名与 run 数，点开 LogDropdown
      - **LogDropdown** (`#log-dropdown`) — 历史日志列表（★ pinned 靠前），含 discard
      - **LogActions** (`#log-rename-btn` / `#log-pin-btn` / `#log-rotate-btn`) — rename / pin / rotate（New log）
  - **Layout** (`.layout`) — 两列网格：Sidebar + MainPanel，吃满 TopBar 以下高度
    - **Sidebar** (`aside.sidebar`) — 左侧导航，三分区，**自带内部滚动**
      - **SessionList** (`#sess-list`) — 会话池现状（跨 run 存活的连接）+ 分栏勾选
        - **SplitToggle** (`#split-toggle`) — Sessions 段头开关：主区单列 ⇄ 按 session 分栏
        - **SessionCheck** (`.sb-check`) — 分栏模式下每条 SessionItem 的勾选标记
      - **ScriptList** (`#scripts-list`) — 已发现脚本；条目右侧 RunAction
        - **ScriptItem** (`li.sb-item`) — 单个脚本条目
          - **RunAction** (`button.sb-action.action-run`) — ▶ enqueue，悬停显现
      - **RunList** (`#runs-list`) — 当前查看日志组内的 run；含 StatusBadge
        - **RunItem** (`li.sb-item`) — 单个 run 条目
          - **RunItemAction** (`button.sb-action`) — 状态化操作
            - `.action-stop` — 停止按钮
            - `.action-rerun` — 重跑按钮
            - `.action-stopping` — 停止中状态
          - **StatusBadge** (`.badge.status-*`) — run 状态色块
    - **MainPanel** (`main.main-panel`) — 主展示区，两态互斥
      - **MainToolbar** (`#main-toolbar`) — 仅 Run detail 态出现
        - **BackToLiveButton** (`#back-to-live`) — 切回常驻 LiveView
        - **MainContextLabel** (`#main-context`) — 当前在看哪个 run
      - **LiveView** (`#live-root`) — 活跃日志组实时尾部（`LiveTail` 实例）
        - **LiveHeader** (`.run-header`) — 日志名 + 行数/等待提示；sync 锁定时显示 `synced @ HH:MM:SS`
        - **PaneContent** (`.pane-content`) — PaneBody 与 ChronoRail 并排的一层
        - **LiveBody** (`ol.live-body`) — LiveView 的 PaneBody；追加式渲染，超 `LIVE_MAX_ROWS` 从头裁剪
        - **ChronoRail** (`.tl-chrono-rail`) — 每列右侧按事件密度归一化的时间轴（见下节）
          - **Tick** (`.tl-tick`) — 一格 = 一个 anchor 事件；点击触发跨列 snap sync
        - **SyncTargetRow** (`.tl-sync-target`) — snap sync 后本列命中的行，落在 viewport 40% 高度
        - **RunSeparator** (`li.run-separator`) — 流内 run 边界标记，由 `workspace.run.begin/end` 元行驱动
        - **CommandBar** (`.command-bar`) — 手动命令输入终端（固定在 LiveView 底部）
          - **SessionSelect** (`#cmd-session-select`) — 下拉选择目标 session
          - **CommandInput** (`#cmd-input`) — 命令输入框（Enter 执行，上/下浏览历史）
          - **ExecuteButton** (`#cmd-send-btn`) — 执行按钮
          - **HistoryButton** (`#cmd-history-btn`) — 历史下拉按钮
      - **RunDetailView** (`#timeline-root`) — 单 run 回放（`Timeline` 实例）
        - **RunHeader** (`.run-header`) — 脚本名 / run id / 状态 / 异常
        - **PlaybackToolbar** (`.timeline-toolbar`) — Live·Play·Reset·Step·SpeedSelect·Scrubber·TimeLabel
        - **PaneSet** (`.pane-set` / `.split`) — 日志行的落地容器；单列或按 session 多列
          - **Pane** (`.pane`) — 一列 = 一个 session
            - **PaneHeader** (`.pane-header`) — 表头显示会话标签
            - **PaneBody** (`ol.timeline-body`) — **内部滚动区**；每列各自滚动与 auto-scroll
              - **LogRow** (`li.tl-row`) — 一条记录
                - **TsCell** (`.tl-ts`) — 时间戳
                - **EventCell** (`.tl-ev`) — 事件类型
                - **SessionCell** (`.tl-sid`) — 会话 ID
                - **MsgCell** (`.tl-msg`) — 消息内容
          - **ScriptPane** (`.pane.pane-script`) — 兜底列：脚本自身的 `user_log` 与 RunSeparator

前端状态字段同样固定：`state.view`（`live` \| `run`）、`state.liveBuffer`（挂活跃日志）、`state.viewedLogId`（RunList 展示范围）、`state.currentRunId`（服务端在跑的 run，不抢主区）、`state.split`（`{enabled, sessionIds}`，持久化在 `localStorage["rpm.split"]`）。

**AppShell 布局约束**：

- 整页**不出现文档级滚动条**：`body.app-shell` 锁 `100vh` 且 `overflow: hidden`，纵向 flex 分 TopBar（不收缩）+ Layout（`flex: 1; min-height: 0`）；
- 滚动只发生在三个内部容器：Sidebar、LiveBody、TimelineBody。每一级 flex/grid 子项都要显式 `min-height: 0`，否则内容会把容器顶大、`overflow-y: auto` 不生效；
- **禁止**用 `calc(100vh - <常数>)` 顶掉 TopBar 高度——TopBar 高度随内容与换行变化，硬编码必然溢出；
- 视口锁定规则**只挂在 `body.app-shell` 上**：`styles.css` 同时被静态 HTML 报告复用，报告页是普通的文档流滚动页面，不能被锁死；
- LogRow 列宽随视口自适应（SessionCell 用 `clamp()` 收缩、窄窗口下整列隐藏）；`.tl-msg` 显式占位到消息列，避免无 `session_id` 的记录错位到 SessionCell。

### ChronoRail：按事件密度归一化的时间轴 + 跨列同步

LiveBody 每列右侧有一条窄轨 ChronoRail，解决"多个 session 各滚各的、想看同一时刻发生了什么只能靠肉眼对时间戳"的问题。

**坐标系是事件密度而非真实时间**。tick 的 y 坐标 = 该 anchor 在**全局 anchor 序列**里的下标 / 总数（CDF 归一化），不是 `(ts - tMin) / (tMax - tMin)`。这样：

- 长静默段（`sleep 300`）在 CDF 上几乎不推进，自动收缩成一条线，不占版面；
- 突发段（一秒几百行）在 CDF 上陡增，自动被拉开，看得清；
- 视觉密度天然贴合信息密度，与 LogRow gutter"只有 anchor 才填时间戳"是同一套取舍。

**跨列对齐靠共享坐标系**。`LiveTail._anchorEvents` 是**所有列 anchor 的并集**（按 ts 有序，上限 `ANCHORS_MAX`，超出从头裁剪），所有列的 rail 都用这一份做 y 映射，所以同一个 ts 在每列 rail 上落在**同一高度**。每列 rail 只画属于自己的 tick 子集（`sessionId` 匹配，Script 兜底列收无 session 的），于是列间疏密差异如实反映各 session 的活跃度——这是要保留的信号，**不做**逐列归一化。

**anchor 的判定与 gutter 时间戳同源**（`isAnchorEvent`）：`command_start` / `transfer_start` / `transfer_error` / `session_open` / `session_closed` / `session_error` / `command_error`，以及非 INFO 的 `command_end`。

**snap sync**：点 rail 上任意一格 tick（或空白处，按 y 比例反解到最近 anchor）触发 `_syncToTs(ts)`——每列各自找 `data-ts` 最接近的行（`LogRow` / `RunSeparator` / 弱对齐 shadow marker 都带 `data-ts`），滚到各自 viewport 的 `SYNC_VIEWPORT_RATIO`（40%）高度并打 `.tl-sync-target` 高亮。40% 是"上方留够上下文、下方留够看接下来发生什么"的折中。

**锁定与解除**：sync 后所有列 `userScrolled = true`，tail 冻结（否则新记录一到就把对齐位置顶走），LiveHeader 显示 `synced @ HH:MM:SS`。任意一列滚回底部、或 `scrollToEnd()` / 换日志组，锁定解除、恢复 tail。

**实现约束**：

- rail 是 `LiveTail` 专属（`PaneSet` 的 `rail: true` 选项）。Run detail 已有 playhead + Scrubber，不建第二根时间轴，其 pane DOM 不受影响；
- 增量维护：数据侧只有"尾部追加 anchor"与"头部裁剪"两种 mutation；渲染侧按 y 像素**分桶**（一个像素桶只留严重度最高的那条），DOM 节点数被钉在 rail 高度量级、与 anchor 总数无关，因此全量重排也很便宜。重绘走 `requestAnimationFrame` 合并，窗口 resize 走防抖重排；
- 重放兼容：分栏新增列时 `setPanes` 会重放 buffer，`_recordAnchor` 按 `(ts, sessionId)` 去重，anchor 不会翻倍。

### Web UI

`redpymake serve` 是 `Workspace` 的一个前端。要求：

- FastAPI + Jinja2 SSR + htmx（局部刷新）+ alpine.js（前端小状态）+ WebSocket（实时推送）；
- 前端资源（`htmx.min.js` / `alpine.min.js` / `styles.css` / `timeline.js`）**内联进 wheel** 的 `_web/static/`——不走 CDN；
- 默认 `--host 127.0.0.1 --port 8765`；显式 `--host 0.0.0.0` 时向 stderr 打一行公网风险警告；
- 启动即另起一份活跃日志（上一份为空则复用），`--resume-log` 续用上次；`--no-open` 关闭自动开浏览器；
- 无内置认证（第一版）；
- 关键路由：
  - `GET /`：合并的 workspace 主页；侧栏三段 = sessions / scripts / runs，主区 = Live tail / Run detail 两态；
  - `GET /api/scripts`：`Workspace.discover()` 的 JSON；
  - `GET /api/sessions`：`Workspace.sessions()` 摘要；
  - `GET /api/runs`：`Workspace.runs` 摘要；
  - `POST /api/runs`（body: `{"path": "..."}`）：`Workspace.enqueue` 并返回 `run_id`；
  - `GET /api/runs/{rid}/records`：该 run 的记录（从所属 log 的 stream 里过滤）；
  - `POST /api/runs/{rid}/stop`：`Workspace.stop_current()`，仅当 `rid` 是当前 run；
  - `POST /api/runs/{rid}/rerun`：`Workspace.rerun(rid)`，返回新 `run_id`；
  - `DELETE /api/runs/{rid}`：`Workspace.cancel_run(rid)`，仅对 queued 有效；
  - `POST /api/runs/stop`：`Workspace.stop_current()`（不带 rid 的旧入口，保留）；
  - `POST /api/commands`：执行手动命令，通过 WebSocket 流式推送输出；
  - `GET /api/commands/history`：获取命令历史；
  - `DELETE /api/commands/history`：清空命令历史；
  - `GET /api/logs`｜`/api/logs/current`｜`/api/logs/{lid}`｜`/api/logs/{lid}/runs`｜`/api/logs/{lid}/runs/{rid}/records`；
  - `PATCH /api/logs/{lid}`（rename）｜`POST /api/logs/rotate`｜`POST /api/logs/{lid}/pin`｜`DELETE /api/logs/{lid}`；
  - `WS /ws`：多路复用 sessions / runs / log 变更 / 活跃日志的记录增量；
- 核心库依赖零 UI；Web 走 `redpymake[web]` extra；
- `Workspace` 是 `serve` 唯一的状态所有者，路由处理器**只读 workspace 状态、只调 workspace 指令**，不引入独立 UI 层业务状态。

### 静态 HTML 报告

`redpymake report NDJSON -o report.html`：

- 读入一份 NDJSON，输出一个自包含 HTML；
- 页面结构 = Web UI 主区（时间线 + 命令详情）的**只读快照版本**；
- 数据以 `<script type="application/json" id="run-data">...</script>` 内嵌；
- 前端脚本从该节点读并渲染，**不发任何网络请求**；
- 页面尾部保留 `<pre>` 原始 NDJSON，方便机器解析。

### 明确不做的能力

- 并行运行多脚本（第一版仅串行队列）；
- 主动"抢焦"当前 run 之外的 UI 面板（主区永远跟随 `workspace.current_run`）；
- 桌面 App（PyQt / Tauri / Electron）与 TUI（`rich` / `textual`）—— 一律不做；
- Diff / 回放对比 —— 后续版本；
- 内置身份认证 / 权限系统 —— 后续版本，需要时用反代解决。

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
14. `rpm.discover(root)` 能返回目录内所有 `rpm_*.py` 的 `ScriptCard` 元数据；纯 AST 分析，不 import；语法错误文件不使整个函数崩溃。
15. 设置 `REDPYMAKE_LIVE_SINK=file://...` 后，`ScriptRun` 在生命周期内把每条 `SessionLogRecord` 追加到目标 NDJSON；首尾各有 `script.begin` / `script.end` 元行；异常路径 `script.end.exception` 非空；同时按 `session_id` 镜像一份到同目录 `sessions/<slug>.ndjson`（无 `session_id` 的行进 `__script__.ndjson`），主文件内容不因分文件而改变。
16. `rpm.workspace(root)` 能串行运行多个脚本，脚本代码中的 `rpm.wsl()` / `rpm.ssh(...)` 等在 workspace 作用域内自动借出共享会话（无需修改脚本源码）；`__exit__` 关闭所有会话；模块每次 `▶` 自动 reload。
17. `Workspace` 具备**日志组**：**一份日志 = 一份 `stream.ndjson`**，该日志下所有 run 的记录顺序追加进同一文件（不做 per-run 分片），并旁路镜像出 `sessions/<slug>.ndjson` 供按会话查看；run 摘要从 `workspace.run.begin/end` 元行重建，孤立 begin 重建为 `interrupted`；用户可 rename / rotate / pin / discard；重开 workspace 时自动恢复上次活跃日志 + 其历史 runs（`serve` 的启动语义见第 20 条）；rotate 遇到 running run 时拒绝并给出明确错误。
18. Web UI 主区分 **Live tail** 与 **Run detail** 两态：live buffer 挂钩**活跃日志**跨 run 持续累积，切去看历史 run 不打断 live，可随时切回并看到完整尾部；Sidebar Runs 每条按状态自动呈现 `⏹`（queued/running）或 `⟳`（终态）操作按钮；`stop_current` 是尽力而为的协作式取消（下一条命令边界生效，不强杀在跑的子进程）。
19. Web UI 页面吃满视口、**无文档级滚动条**：`body.app-shell` 锁 `100vh`，滚动只出现在 Sidebar / LiveBody / TimelineBody 三个内部容器；样式表不含 `calc(100vh - <常数>)` 式的 TopBar 高度硬编码；视口锁定只作用于 `body.app-shell`，静态 HTML 报告页保持普通文档流；隐藏态的 RunDetailView 不占位（ID 选择器不得盖过 `.panel-view[hidden]`），LiveView 独占 MainPanel 全高。
20. `redpymake serve` 每次启动另起一份活跃日志（上一份 `run_count == 0` 时复用），旧日志留在 `list_logs()` 与 LogDropdown 里可回看；`--resume-log` 续用上次；`rpm.workspace(root)` 默认行为不变。
21. Web UI 支持按 session 分栏：SplitToggle 开启后 SessionList 的勾选项各占一列，脚本自身日志与 RunSeparator 进 ScriptPane；Live 与 Run detail 两态都可分栏，且回放时多列共享**同一个**播放头；勾选持久化到 `localStorage`。
22. LiveBody 每列右侧有 ChronoRail：tick 的 y 按**全局 anchor 序列的排名 / 总数**（CDF）定位而非真实时间比例，长静默自动收缩、突发段自动展开；所有列共用同一份全局 anchor 表，同一个 ts 在各列落在同一高度；点任意 tick 触发跨列 snap sync，各列滚到最接近该 ts 的行并对齐到 viewport 40% 高度，其间 tail 冻结、LiveHeader 提示锁定点，滚回底部即恢复。Run detail 不建 rail（已有 playhead）。

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
| 脚本发现 | 二级接口组 E：文件名前缀 `rpm_*.py`（`pyproject.toml [tool.redpymake.discovery] patterns` 可覆盖）+ AST 抽取元数据；不 import 目标脚本 |
| 脚本身份 | 运行时依旧 `with rpm.script(...)`（CORE-09）；前缀只是文件级发现的锚点 |
| 实时日志 sink | 环境变量 `REDPYMAKE_LIVE_SINK=file://...`；仅 file:// 一种传输；首/尾有 `script.begin` / `script.end` 元行；与 `dump_on_error` 正交 |
| CLI 入口 | `redpymake discover` / `run` / `report` / `serve`；核心库零 UI 依赖，Web 走 `[web]` extra |
| Workspace 会话池 | 懒创建 + 按 key 缓存 + 跨脚本复用；`__exit__` 时默认 `auto_close_sessions=True` 关全部 |
| Workspace 执行模型 | 单线程串行队列 + `enqueue` 随时可加；`importlib.reload` 每次 ▶ 生效；主区跟随 `current_run` |
| 脚本入口约定 | 必须 `def main() -> None`；`if __name__ == "__main__": main()` 保持 CLI 直跑等价 |
| Web 技术栈 | FastAPI + Jinja2 SSR + htmx + alpine.js + WebSocket；无前端构建链；资源内联入 wheel |
| Workspace 日志组 | 一份 workspace 恒有一个活跃日志；用户可 rename / rotate（=清空）/ pin / discard；跨 `serve` 实例自动恢复；`redpymake run`（CLI 独立子进程）不走这套 |
| 日志存储粒度 | **一份日志 = 一份 `stream.ndjson`**（该 log 下所有 run 顺序追加进同一文件）；**不做** per-run / per-script 分片；一份日志即一个自包含可 `report` 的 NDJSON |
| 日志目录布局 | `<logs_root>/<log_id>/{meta.json, stream.ndjson, sessions/}`；活跃日志由 `_active.json` 指定；无 `runs.index.jsonl`、无 `runs/` 子目录 |
| 按 session 分文件 | `sessions/<slug>.ndjson` 是 sink 的**旁路镜像**，只写不读；`stream.ndjson` 始终是权威合并流；无 `session_id` 的行进 `__script__.ndjson`；文件名对 `session_id` 做安全化 + 冲突时加 sha1 短后缀 |
| Run 摘要来源 | 不单独持久化；从 stream 里的 `workspace.run.begin` / `workspace.run.end` 元行配对重建；孤立 begin → `status="interrupted"` |
| run 边界元行归属 | CORE-10 sink 写 `script.begin/end`；`workspace.run.begin/end` 由 Workspace 在 sink 外侧追写，携带 `run_id` 等 workspace 专属元数据；两类元行都无 `session_id`，镜像时一并进 `sessions/__script__.ndjson` |
| Rotate 冲突处理 | 有 run 在跑时 `rotate_log` 拒绝并抛 `RuntimeError`；UI 提示用户等或终止；后续版本考虑排队式 rotate |
| Stop 语义 | 协作式尽力而为：设标志 + 借出会话在 `run()`/`wait()` 边界抛 `WorkspaceStoppedError`；**不强杀**在跑的子进程；run 终态覆盖为 `cancelled` |
| Web UI 主区 | 两态：Live tail（挂钩活跃日志、跨 run 持续、可随时切回）与 Run detail（单 run 回放，含 playback 控件）；主区只展示不操作 |
| Web UI 运行控制 | 全部收敛到 Sidebar 行内：Scripts 条目 `▶`、Runs 条目按状态 `⏹`/`⟳`；不做 Script 详情卡；Rerun 不弹确认；活跃 run 不自动抢占主区 |
| Web UI 部件命名 | 固定一套 DOM 部件名（AppShell / TopBar / Sidebar / MainPanel / LiveView / RunDetailView…），文档与讨论统一使用 |
| Web UI 页面滚动 | 视口锁定：`body.app-shell` 占满 `100vh` 不滚动，滚动条只在 Sidebar / LiveBody / TimelineBody 内部；禁止 `calc(100vh - 常数)` |
| serve 启动日志 | 每次 `serve` 另起一份活跃日志（上一份为空则复用），`--resume-log` 续用；库内 `rpm.workspace()` 默认仍续用上次 |
| 多 session 查看 | 按 session 分栏，列定义走 PaneSet；Live 与 Run detail 都支持，回放共享单一播放时钟；脚本自身日志进 ScriptPane |
| LiveBody 时间轴 | ChronoRail 按**事件密度 CDF**定位 tick（非真实时间比例），空档自动收缩、突发自动展开；全局 anchor 表跨列共享 → 同 ts 同 y；列间疏密差异保留为活跃度信号，不做逐列归一化 |
| 跨列时间同步 | 点 ChronoRail 的 tick 做 snap sync：各列滚到最接近该 ts 的行、对齐 viewport 40%；同步期间冻结 tail，滚回底部恢复。**不做**持续拖动跟随（反馈环 + 稀疏列抖动） |

---

## 7. 后续版本（非本草案范围）

- 内容哈希增量策略
- 连接重试与退避
- DAG 编排与原生 async
- 远程进程 spawn / wait / kill
- TFTP 作为 optional extra
- Workspace 并行队列 / diff 视图 / 内置认证
- Workspace 日志组的**排队式 rotate**（当前 rotate 遇 running run 会直接拒绝）
- Workspace 日志组的跨机同步 / 云端归档
- 实时 sink 的 `stdout://` / `tcp://` 变体
