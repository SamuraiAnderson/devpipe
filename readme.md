# RedPyMake

**功能定位：** RedPyMake 是一套以 Python 为任务脚本的跨平台自动化框架，并在同一套模型上提供 Web 可视化：用统一抽象管理「会话连接、文件与远端资源、脚本化任务」，在界面中浏览脚本、静态展示脚本中的终端/服务拓扑，并运行脚本、分源查看日志。

## 能力与代码落点

| 能力 | 含义 | 代码 |
| ---- | ---- | ---- |
| 会话连接 | 本地 / SSH / ADB / 串口等统一命令执行与日志 | [`src/BaseControl.py`](src/BaseControl.py) 及各平台实现（如 `LocalHost`、`Linux`、`AdbCnet`、`SerialControl`） |
| 文件表示 | 跨端传输、路径与增量语义 | [`src/file.py`](src/file.py) 中 `UFile`，与控制器上的 push/pull 等配合 |
| 脚本编写 | 用普通 Python 组织任务；可选 Make 风格增量 | [`src/make_style.py`](src/make_style.py)；示例在 [`example/`](example/) |
| 脚本可视化 UI | 脚本树、AST 卡片、多源日志 | [`ui/app.py`](ui/app.py)，分析见 [`ui/services/script_analysis.py`](ui/services/script_analysis.py) |

**可视化（现状）：** 不执行脚本即可从源码了解会实例化哪些控制器/服务及构造参数（卡片展示），并配合多 Tab 日志。**规划中的** DAG/流程图编排等见 [`doc/TODO.md`](doc/TODO.md)。

## 启动 Web 界面

从项目根目录执行：`streamlit run ui/app.py`

## 注意事项

SFTP 不支持 `~`，因此 Linux 连接配置中请勿依赖 `~` 表示家目录。
