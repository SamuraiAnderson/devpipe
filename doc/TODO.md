# RedPyMake TODO

**基准：** [`doc/core-lib-requirements.md`](core-lib-requirements.md) v0.5
（core-lib 第一版 = 已合并 `9ce30b7`）

优先级分层：**P0** 是第一版打磨（缺失 / 待补），**P1** 与需求文档 §7 一致的后续版本能力，**P2** 是长期演进与生态建设。以复选框 `[ ]` 追踪；完成后改为 `[x]` 并保留一行说明。

---

## P0：第一版打磨

> 已合并 `9ce30b7` 后立即可以起手做的事，用于让 v0.5 达到"日常可用"。

### P0-1 examples 与前十分钟场景

- [ ] 重写 `example/`：本地命令、SSH 构建、ADB 推送、串口启动等真实脚本
- [ ] `.env.example` 与 `argparse` 模板，配合 `python-dotenv`（作为 optional，不进核心依赖）
- [ ] README 前十分钟示例对齐 `example/`，确保可复制粘贴运行（对应 §5-1 验收）

### P0-2 集成测试

> 需求文档 §5-8 已声明 "SSH、ADB、串口测试标记为集成测试"，用 `pytest -m integration` 单独运行。

- [ ] `tests/integration/test_ssh.py`：起本地 `sshd` 容器或用 GitHub Actions matrix
- [ ] `tests/integration/test_adb.py`：ADB emulator 或跳过策略（`pytest.importorskip`）
- [ ] `tests/integration/test_serial.py`：`pyserial.tools.loopback` 或 tty0tty
- [ ] `_session.py` 的传输错误分支（本地→远端失败重试、`_copy_via_local_tmp` 中转）需要真实通道才能覆盖

### P0-3 覆盖率补齐

当前默认单测覆盖率 ≈87%，以下坑位可以在不引入设备的前提下补：

- [ ] `_session.py` 视图关闭、跨会话 copy 校验、shell=True + env 传播（当前 79%）
- [ ] `_path.py` 的 `~` 展开、Windows 驱动符 `parent` 边界、`refresh()` 契约测试
- [ ] `_stale.py` 自定义谓词抛异常时的日志落盘

### P0-4 CI 与发布准备

- [ ] GitHub Actions：`pytest` matrix (Windows / macOS / Linux × Python 3.10/3.11/3.12)
- [ ] `pre-commit`：`ruff` + `black` + `mypy --strict src/redpymake`
- [ ] `pyproject.toml` 补 `[project.urls]` 完整字段与 `LICENSE` 文件
- [ ] `pip wheel .` 构建校验；`twine check dist/*`；预留 PyPI 发布流水线

### P0-5 类型收敛

- [ ] 消除 `Any`：`_stale._as_iterable`、`_session._as_path`、`exceptions.*` 的 `Any` 字段收窄
- [ ] 为 `Session` 的 hooks 提供 `Protocol` 类型别名，便于第三方扩展平台

### P0-6 文档

- [ ] 每个 `_session.at()`/`.run()`/`.push()` 都要有 docstring 示例并挂 `doctest`（本地示例可直接跑）
- [ ] 生成 API 文档（Sphinx 或 MkDocs + Griffe）
- [ ] "如何添加新平台" 教程：实现 `Session` 抽象钩子的最小样例

---

## P1：需求文档 §7 后续版本

### P1-1 内容哈希增量策略 `strategy="hash"`

> 需求文档 §CORE-05 保留了接口，当前抛 `UnsupportedOperationError`。

- [ ] 落地 `blake3` / `sha256` 双候选，默认 blake3（作为 optional extra `[hash]`）
- [ ] 缓存清单：`.redpymake/hash.json`（路径 → (mtime, size, digest)），命中则跳过 rehash
- [ ] 跨设备一致：远端通过 `sha256sum` / `busybox sha256sum` 探测；无则抛清晰的 `UnsupportedOperationError`
- [ ] 目录哈希：按内容树聚合（对齐 `mtime` 策略的"仅顶层"局限）
- [ ] `stale.check` 日志字段扩展：`digest_source`、`digest_target`

### P1-2 连接重试与退避

> 需求文档 §2 明确本版不做，作为后续版本能力。

- [ ] `rpm.ssh(..., retry=Retry(attempts=3, backoff="exponential", base=0.5, max=10))`
- [ ] 覆盖：初次 `connect()`、`open_session()`、SFTP 通道级；不覆盖 `run` 内部业务失败
- [ ] ADB 掉线（`error: device 'xxx' not found`）识别与重连
- [ ] 每次重试写入 `session.retry` 日志事件，包含 attempt、reason、backoff

### P1-3 DAG 编排 + 原生 async

> 需求文档 §7 提及；这一节比其他 P1 更改动大，需要独立设计文档 `doc/dag-design.md`。

- [ ] 引入 `AsyncSession` 抽象，与同步 `Session` 二选一（不做 hybrid）
- [ ] `rpm.pipeline(...).node("build", fn).node("deploy", fn, depends_on=["build"])`
- [ ] 基于 `asyncio.TaskGroup`（3.11+）的拓扑并发执行
- [ ] 与 `rpm.stale` 深度集成：整条 DAG 支持 `skip_when_stale=False`
- [ ] 保留同步入口不动，async 走独立命名空间 `rpm.aio.*`

### P1-4 远程进程生命周期 `spawn` / `wait_pid` / `kill`

> 用于 C/S 场景：板端后台启动 → 本地拉流 → 清理。

- [ ] `session.spawn(argv, name="server")` 返回 `RemoteProcess`（非 `CommandResult`）
- [ ] `RemoteProcess.wait_ready(pattern, timeout)` 复用 `logs.wait` 机制
- [ ] `RemoteProcess.kill()` 幂等；`with session.spawn(...) as proc:` 自动清理
- [ ] SSH：通过 `nohup` + 独立通道；ADB：通过 `run-as` 或 shell 后台

### P1-5 TFTP 作为 optional extra

- [ ] `redpymake[tftp]` 只暴露 `rpm.tftpd(root_dir, host, port)` 上下文管理器
- [ ] 与主 `Session` 解耦；作为独立辅助设施而非 Session 子类

---

## P2：生态与长期

### P2-1 Web UI 基于日志订阅接口重建

> 需求文档 §7：**基于新会话日志订阅接口重建**，不复用旧 Streamlit 代码。

- [ ] 独立仓库 `redpymake-web`（或本仓库 `web/` 子包），依赖 `redpymake>=0.6`
- [ ] 通过 `session.logs.subscribe(...)` 拿实时事件流，前端渲染
- [ ] AST 脚本分析（可选）：静态卡片 + 动态运行侧栏，两者用同一份 `SessionLogRecord` 数据模型
- [ ] 不再从核心库反向暴露"UI 友好的" API；核心 API 只负责日志正确产出

### P2-2 平台扩展

- [x] `WslSession`（Windows 上的 Linux 子系统）— 通过 `wsl.exe` 子进程；`push`/`pull` 走 `/mnt/<drive>/…` 中转
- [ ] `WinRMSession`（Windows 远端）
- [ ] `KubernetesPodSession`（`kubectl exec`）
- [ ] `DockerSession`（`docker exec`）
- [ ] `TelnetSession`（老旧设备）

### P2-3 观测与运维

- [ ] `session.logs.export(fmt="ndjson"|"otlp")`：为 OpenTelemetry / Loki 铺路
- [ ] `stale.check` 事件的 Prometheus 指标导出（命中率、耗时）
- [ ] 结构化 trace：`operation_id` → 分布式追踪的 span id 映射

### P2-4 SDK 与脚手架

- [ ] `redpymake init <project>`：生成 `pyproject.toml` + `example/` + CI 骨架
- [ ] Cookiecutter 模板：常见场景（板级自动化测试、跨机构建流水线）
- [ ] IDE 提示：`.pyi` stub 增强类型体验（虽然已 `py.typed`，但复杂泛型需要显式 stub）



---

## 变更记录

- 2026-08-15：新建；基于 v0.5 需求与 `9ce30b7` 实现现状拟定
- 2026-08-15：新增 `WslSession` + `rpm.wsl(...)` 工厂（CORE-01 显式例外：只校验 `wsl.exe` 存在，不做 distro 级探测）；集成测试在 `RPM_TEST_WSL=1` 门控下运行
- 2026-08-15：新增 CORE-09 脚本对象与日志分流：`rpm.script(name, dump_on_error, log_level, loggers)` + `ScriptRun` + `ScriptSnapshot`；`logging` 通过 handler 桥 + Session `subscribe` 转发合流到 `ScriptRun._merged`；`__exit__` 见异常时按 `dump_on_error` 值类型（单文件 / 目录包 / callable）自动落盘，不吞原异常
