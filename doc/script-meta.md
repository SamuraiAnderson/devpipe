# 脚本元语义参考

RedPyMake 的 Web UI 通过 **AST 静态分析**和**运行时约定**从 `example/` 任务脚本中
提取超出普通 Python 语义的信息。本文档汇总所有已支持的元语义。

> 实现入口：`ui/services/script_analysis.py`（AST）、`ui/services/script_service.py`（运行时）

---

## 1. 入口函数（运行时约定）

`ScriptService` 在后台线程中 `import` 脚本模块后，按以下优先级查找入口：

1. `make_test()` — 最高优先
2. `main()` — 次优先
3. 整文件 `exec` — 兜底

```python
# 脚本只需定义其中一个即可
def make_test():
    ...

def main():
    ...
```

来源：`ui/services/script_service.py` — `ScriptService._worker()`

---

## 2. 控制器 / 服务实例化（AST 静态分析）

UI 在选中脚本后立即进行 AST 分析，提取已知类的实例化信息，**不执行脚本代码**。

### 已知类

| 类名 | 平台标识 | 类别 | 位置参数 |
|---|---|---|---|
| `AdbCnet` | Android | controller | host, user |
| `Linux` | Linux | controller | host, user |
| `LocalHost` | Local | controller | (无) |
| `SerialControl` | Serial | controller | port, baudrate, timeout, mode |
| `TftpdServer` | TFTP | service | root_dir, host, port |

### 支持的实例化形式

```python
# 赋值
linux = Linux("192.168.1.1", "root")

# 带类型注解赋值
linux: Linux = Linux("192.168.1.1", "root")

# with 上下文
with Linux("192.168.1.1", "root") as linux:
    ...

# 裸调用表达式（无变量名）
Linux("192.168.1.1", "root")
```

### 提取的信息

- `class_name` — 类名（如 `"Linux"`）
- `platform` — 平台标识（如 `"Linux"`）
- `var_name` — 赋值目标变量名（无则为 `None`）
- `params` — 构造参数的字面值（best-effort 提取）
- `kind` — `"controller"` 或 `"service"`

### 派生属性

- `key` — 去重标识，格式 `"platform:var_name"` 或 `"platform"`
- `log_source` — 日志路由 key，格式 `"platform.host"`（如 `"Linux.192.168.1.1"`）

### 用途

- 生成动态日志 Tab（每个 controller 一个独立 Tab）
- 可视化面板中的实例卡片

来源：`ui/services/script_analysis.py` — `analyze_script()`、`_InstantiationVisitor`

---

## 3. LOG_FILTERS 日志过滤（AST 静态分析）

在脚本**模块顶层**声明 `LOG_FILTERS` 字典，为日志 Tab 配置基于正则表达式的
include（白名单）/ exclude（黑名单）过滤规则。

### 声明格式

```python
LOG_FILTERS = {
    "<key>": {"include": r"<regex>", "exclude": r"<regex>"},
    ...
}
```

### key 类型

| key | 含义 |
|---|---|
| 控制器变量名 | 映射为该控制器的 `log_source`（如 `"linux"` → `"Linux.192.168.1.1"`） |
| `"Script"` | 固定的「脚本」Tab |
| `"All"` | 固定的「全部」Tab |
| `"*"` | 通配符，对所有 Tab 生效 |

### 过滤语义

- `include` — 仅显示 message 匹配该 regex 的日志行
- `exclude` — 隐藏 message 匹配该 regex 的日志行
- 同时设置时，先 include 再 exclude
- `"*"` 规则与 per-tab 规则**叠加**（include 做 OR 合并，exclude 做 OR 合并）

### 示例

```python
LOG_FILTERS = {
    "linux":  {"exclude": r"heartbeat|ping"},   # linux 控制器 Tab 中隐藏心跳日志
    "board":  {"include": r"ERROR|WARN"},        # board 控制器 Tab 中只看错误和警告
    "Script": {"exclude": r"DEBUG"},             # 脚本 Tab 中排除 DEBUG 行
    "*":      {"exclude": r"trivial"},           # 所有 Tab 都排除包含 trivial 的行
}
```

### 设计要点

- 过滤仅在 **UI 渲染层**执行，`LogBuffer` 环形缓冲和磁盘日志文件保留完整数据
- AST 提取只处理**字面量字符串**，不执行脚本代码
- regex 编译失败时记录警告并跳过该规则，不阻塞 UI

来源：`ui/services/script_analysis.py` — `extract_log_filters()`；
`ui/components/log_panel.py` — `_active_sources()`、`_apply_filters()`
