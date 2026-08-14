# WSL 会话示例

这个目录里的脚本演示 `rpm.wsl(...)` 的完整常用面：会话构造、`at()` 视图、
`run()`、文件传输（含 Windows 路径自动映射）、`rpm.stale` 增量、`run().wait()`
日志匹配、`rpm.script(...)` 脚本对象。

## 前置

- **Windows** + 已安装 **WSL**（`wsl -l -v` 至少能看到一个发行版）。
- 在仓库根安装本项目：`pip install -e .`。
- 非 Windows 平台运行 `rpm.wsl()` 会直接抛 `SessionConnectionError`（`wsl.exe`
  找不到），这符合 CORE-01 的连接契约。

## 脚本清单

| 脚本 | 覆盖点 |
| --- | --- |
| [`01_hello.py`](01_hello.py) | `rpm.wsl()` 构造、`at()` 视图、`run()` 结果、`sh.logs.records()` |
| [`02_transfer.py`](02_transfer.py) | `push` / `pull` 字节往返；Windows 路径 → `/mnt/<drive>/` 自动映射 |
| [`03_stale_build.py`](03_stale_build.py) | `rpm.stale(target, depends_on=...)` 只在源比目标新时才"构建" |
| [`04_wait_logs.py`](04_wait_logs.py) | `run().wait(pattern)` 用命令前保存的游标搜索匹配 |
| [`05_script_meta.py`](05_script_meta.py) | `rpm.script(...)` 自动收集会话日志 + 快照 |

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
