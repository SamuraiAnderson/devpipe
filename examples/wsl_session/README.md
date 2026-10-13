# WSL 会话示例

这个目录里的脚本演示 `rpm.wsl(...)` 的完整常用面：会话构造、`at()` 视图、
`run()`、文件传输（含 Windows 路径自动映射）、`rpm.stale` 增量、`run().wait()`
日志匹配、`rpm.script(...)` 脚本对象。

`rpm_*.py` 脚本采用 CORE-10 的**发现约定**，可以被 `redpymake discover` /
`redpymake serve` 静态识别并从 Web UI 触发。`background_probe.py` 是单独运行的诊断脚本。

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

## shell、cwd 与 env 的组合

`shell=False`（默认）将每个参数按字面传递，空格、引号、`$VAR`、通配符都不展开。
`shell=True` 只接收一条完整脚本，按 POSIX `sh` 语法解析，不读取交互式 shell 配置。
WSL 通过 `--exec sh -c` 启动，避免发行版默认 shell 再次解释参数。

`cwd` 的优先级是本次 `run(cwd=...)` > `at()` > 会话默认值。库先切目录、导出 `env`，
再单独解析整段脚本，因此 `;` / `||` / `&` 不会把前置设置截断。目录不存在时，
脚本的任何部分都不会运行；环境变量在脚本展开前就生效，也会传给子进程。
解释器在覆盖环境之前定位，因此新 `PATH` 即使不含 `sh`，内建命令仍能执行。

```python
import redpymake as rpm

with rpm.wsl() as linux:
    result = linux.run(
        'printf "%s\\n" "$MODE"; sh -c \'printf "%s\\n" "$MODE"\' & pid=$!; wait "$pid"',
        shell=True, cwd="/tmp", env={"MODE": "中文 release"}, timeout=10,
    )
    assert result.stdout.splitlines() == ["中文 release", "中文 release"]
```

这是有限任务的显式等待。`wait "$pid"` 的返回值是该任务的状态；如果后面再接其他命令，
shell 最终返回码仍取最后执行的命令。需要传播失败时请保存状态并 `exit "$rc"`。

## WSL 后台进程复现

`run()` 等待本次启动的命令及其输出，不提供后台进程托管。`cmd &` 返回 0 并不表示
子任务启动、就绪或完成。后台任务继承 stdout/stderr 时还可能拖住输出收集；完全重定向
也不能保证 launcher 退出后的存活。长期服务应由发行版内已配置的服务管理器托管，
并另外验证就绪和停止；当前没有 `session.spawn()` 生命周期接口。

```powershell
python examples/wsl_session/background_probe.py
python examples/wsl_session/background_probe.py --distribution Debian
```

脚本输出 Windows / WSL / 发行版 / 内核 / shell 版本，比较显式 `wait`、普通 `&`、
重定向后的 `nohup`、`nohup` 加 `sleep 1`，以及可用时的 `setsid`。
每个子任务先写启动标记，等待 0.4 秒，再写完成标记；Python 侧在 `run()` 返回后等待
0.8 秒再检查。探针在独立临时目录运行，最后仅清理本次创建的文件。

2026-10-10 本机观察（Windows 10.0.26100.2894，WSL 3.0.1.0，Debian 12 bookworm，
内核 `6.18.40.1-microsoft-standard-WSL2`，`/usr/bin/dash`）：

- 显式 `wait`：返回 0，约 0.45–0.47 秒，启动与完成标记均存在。
- 普通 `&`：返回 0，约 0.11 秒；首次未见启动标记，复测出现启动标记，但均无完成标记。
- 重定向后的 `nohup`、`setsid`：返回 0，约 0.09–0.11 秒，两个标记均不存在。
- `nohup` 加 `sleep 1`：返回 0，约 1.06–1.09 秒，两个标记均存在。

这些是该环境的可重复观察，不足以判定终止信号或证明“所有 WSL 都会杀整个进程组”。
最后一项的短任务在 launcher 退出前已有时间完成，不能证明 `sleep 1` 能让任意后台服务
稳定存活。`nohup` / `setsid` 也不能当作跨 WSL 版本的保证。历史类似现象可参考
[Microsoft WSL #5143](https://github.com/microsoft/WSL/issues/5143)。

## 中文输出与错误诊断

`run(encoding="utf-8")` 解码 Linux 命令的输出；只有命令实际产生 GBK 字节时才改用
`encoding="gbk"`。Windows 控制台的代码页不决定 Linux 命令的编码。

WSL 默认调用和内部文件操作在单次 launcher 环境中设置 `WSL_UTF8=1`，避免重定向管道
中的启动器诊断按 UTF-16LE 输出后被错读。此选项来自
[WSL 0.64 发布说明](https://github.com/microsoft/WSL/discussions/8641)；旧版 WSL 需升级。
显式非 UTF-8 的命令编码不保证能同时解码启动器自身的诊断。

CLI 按 stdout/stderr 当前编码写入。在 GBK 流中中文保持可读，emoji 等无法表示的字符
显示为转义；JSON 使用合法转义，解析后还原原文。库的异常字段和日志仍是 Unicode，
不会修改调用进程的全局代码页。自行编写的 `print(exc)` 由脚本配置输出流，CLI 无法
替用户脚本重设终端。
