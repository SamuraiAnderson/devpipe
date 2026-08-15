# WSL 会话示例

这个目录里的脚本演示 `rpm.wsl(...)` 的完整常用面：会话构造、`at()` 视图、
`run()`、文件传输（含 Windows 路径自动映射）、`rpm.stale` 增量、`run().wait()`
日志匹配、`rpm.script(...)` 脚本对象。

所有脚本采用 CORE-10 的**发现约定** `rpm_*.py`，可以被 `redpymake discover` /
`redpymake serve` 静态识别并从 Web UI 触发。

## 前置

- **Windows** + 已安装 **WSL**（`wsl -l -v` 至少能看到一个发行版）。
- 在仓库根安装本项目：`pip install -e .`。
- 非 Windows 平台运行 `rpm.wsl()` 会直接抛 `SessionConnectionError`（`wsl.exe`
  找不到），这符合 CORE-01 的连接契约。

## 直接运行 vs Web UI

三种等价方式，脚本源码**完全一致**：

```powershell
# 1) Python 直跑
python examples\wsl_session\rpm_hello.py

# 2) CLI 跑（顺带落 NDJSON，便于事后 report）
redpymake run examples\wsl_session\rpm_hello.py --sink runs\hello.ndjson
redpymake report runs\hello.ndjson -o hello.html

# 3) Web UI（需要 pip install "redpymake[web]"）
redpymake serve examples\wsl_session
# 会自动用系统默认浏览器打开 http://127.0.0.1:8765
# 不想自动开：redpymake serve --no-open examples\wsl_session
```

**Web UI 的交互（两步式，避免误触）：**

1. 侧栏 `Scripts` 里点脚本 → 主面板显示**详情卡**（脚本名、路径、`factories`、
   `docstring`、`error`）；此时**不会**执行；
2. 详情卡里点 `▶ Run` → 才真正入队；主面板即时切到 **Live** 时间轴，逐行流式
   显示两个 session 的输出；
3. 跑完后 `Runs` 里点历史条目 → 进入 **Replay** 模式，可 `▶/⏸ / ⏮ / ⏭ / 0.5x-10x/Max`
   拖 scrubber 回放整个时间轴。

`redpymake serve` 会用一个 `Workspace` 接管所有会话；`rpm.wsl()` 等工厂在
workspace 作用域内**自动借出共享连接**，同一 distro 只连一次并在多次 Run 之间复用。

## 脚本清单

| 脚本 | 时长 | 覆盖点 |
| --- | --- | --- |
| [`rpm_hello.py`](rpm_hello.py) | ~2s | `rpm.wsl()` 构造、`at()` 视图、`run()` 结果、`sh.logs.records()` |
| [`rpm_transfer.py`](rpm_transfer.py) | ~2s | `push` / `pull` 字节往返；Windows 路径 → `/mnt/<drive>/` 自动映射 |
| [`rpm_stale_build.py`](rpm_stale_build.py) | ~1s | `rpm.stale(target, depends_on=...)` 只在源比目标新时才"构建" |
| [`rpm_wait_logs.py`](rpm_wait_logs.py) | ~2s | `run().wait(pattern)` 用命令前保存的游标搜索匹配 |
| [`rpm_script_meta.py`](rpm_script_meta.py) | ~2s | `rpm.script(...)` 自动收集会话日志 + 快照 |
| [`rpm_long_pipeline.py`](rpm_long_pipeline.py) | ~20s | **本机 + WSL** 双端流水线：数据生成 → push → 长任务 build → wait 匹配 → pull 校验；专为 Live 追流 / Replay 回放设计 |

## 语义要点（会踩坑的地方）

- **构造几乎零延迟**：`rpm.wsl()` 只校验 `wsl.exe` 存在，不预探测发行版。
  distro 未安装 / 冷启动失败会延迟到首次 `run()` 时以 `CommandError` 呈现
  （见 [`src/redpymake/_wsl.py`](../../src/redpymake/_wsl.py) 顶部注释）。
- **路径自动映射**：`sh.push(local.path(r"C:\foo\bar.bin"), remote_path)` 内部
  会把 `C:\foo\bar.bin` 换成 `/mnt/c/foo/bar.bin`。UNC 路径（`\\server\share`）
  不支持转换，会原样交给 `wsl` 侧处理。
- **wait 的游标语义**：`session.wait(pattern)` 默认从"下一条"开始扫描，不会
  匹配到调用之前的历史；要匹配某条命令期间的输出，用 `run(...).wait(pattern)`
  （它带命令启动前保存的游标）。
- **指定 distro / user**：`rpm.wsl(distribution="Ubuntu-22.04", user="root")`
  对应 `wsl.exe -d Ubuntu-22.04 -u root`。
