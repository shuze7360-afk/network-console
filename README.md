# Network Console

Windows 桌面托盘应用：以四张状态卡（**基础网络 / 网络认证 / 代理连接 / 本地服务**）
集中呈现网络可用性，配合静默定时检查与可审计的受限修复。所有需要修改系统设置
的操作都由统一执行器完成（写前读旧值、写后回读、留审计日志），界面不直接改动
任何设置。

**2.0.0 行为边界**：后台与 AI 会话只诊断、提醒与解释；网络修改只能由用户在
控制台界面点击触发。启动、定时检查、快照刷新与通知点击零网络写入；发现的
问题通过托盘提醒一次（带冷却），由你点击对应按钮处理。

- GUI：Python + PySide6，明暗主题跟随 Windows
- 后台：3 分钟静默定时检查，仅在需要人工处理时提醒（故障抖动有冷却）
- MCP：内置 stdio 服务器，可接入支持 MCP 的 AI 助手做只读诊断（2.0.0 起不再代为修复）
- 平台：Windows 10/11（使用 WinINET/netsh/WMI 等系统能力）

## 安装

```bat
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

桌面快捷方式（可选）：指向 `.venv\Scripts\pythonw.exe`，参数 `-m netconsole.ui`，
工作目录设为本目录；（需 Python 3.10+；建议 3.13）。

## 配置

首次运行不需要任何配置：所有可选功能默认停用，卡片显示「未配置/未启用」，
不会扫描或导入其他应用的配置，也不会修改系统代理、DNS、hosts 或自启项。

将 `config.example.json` 复制为 数据目录下的 `config.json`（默认
`%USERPROFILE%\.network-console-app\config.json`）后按需修改：

| 字段 | 说明 |
|---|---|
| `features.auth.login_url` | 网络认证页地址（留空则该卡显示未配置） |
| `features.auth.match_prefixes` | 网络名前缀匹配：命中才做认证探测（如 `["CAMPUS"]`） |
| `features.proxy.endpoint` | 受控代理端点（如 `127.0.0.1:8080`） |
| `features.proxy.program` | 代理程序（path/args/workdir），「开关」用于启停它 |
| `features.service.*` | 本地服务的端点、健康路径与程序 |
| `cleanup.controlled_endpoint` | 残留清理只处理指向该端点的系统代理 |
| `cleanup.bypass_defaults` | 直连例外合并写入的默认条目 |

示例值均为虚构（`example.edu`、RFC5737 文档地址、`C:\Path\To\...`）。
配置文件损坏时应用进入「状态不可确认」保护态，禁止自动写入。

## MCP（AI 助手接入）

仓库内的 `netconsole/mcp_server.py` 是 stdio MCP 服务器：

- `net_status` —— 四链路状态、一句话摘要、待处理事项、系统代理摘要（只读）；
- `net_fix` —— **2.0.0 起不再代为执行修复**：合法 action
  （`cleanup_stale_proxy` / `restore_bypass` / `restore_snapshot`）返回
  `manual-required` 与界面操作指引，拒绝本身留审计；未知 action 返回参数错误。

MCP 不提供会话内启动/停止服务的能力。这是一个不兼容变化：依赖 v1
「调用即修复」行为的集成需要改为把指引转述给用户。

## 隐私扫描

`scripts/privacy_scan.py` 在发布前扫描敏感信息（只输出位置+类别，不回显内容）：

- 通用规则内置（凭据、邮箱、内网 IP、设备名、可疑赋值），扫描器自身也在
  扫描范围内；扫描集为 git 语义的发布候选（跟踪 + 未忽略未跟踪文件）全量
  覆盖，二进制按字节扫描，无法读取的对象列入待处理而非计为通过；
- 与使用者真实身份相关的词表**保存在仓库外**：用 `--private-rules PATH` 或
  环境变量 `NETWORK_CONSOLE_PRIVATE_RULES` 提供（JSON：`{"patterns": {"类别": "正则"}}`），
  缺失时自动降级为仅通用规则，对公开贡献者足够；
- `--history REPO` 对指定仓库做历史审计：逐提交扫描补丁内容、提交消息与
  作者/提交者身份元数据。

## 隐私与边界

- 默认不启动、停止或接管任何服务；探针仅访问你在配置中指定的地址。
- 清理动作只处理明确指向受控端点的残留；指向其他地址的代理一律不动。
- 日志/状态保存在本地数据目录；仓库不含任何真实网络配置或个人数据。

## 测试

```bat
.venv\Scripts\python scripts\test_appconfig.py
.venv\Scripts\python scripts\test_proxyaddr.py
.venv\Scripts\python scripts\test_notify_policy.py
.venv\Scripts\python scripts\test_present.py
.venv\Scripts\python scripts\test_notify_cooldown.py
.venv\Scripts\python scripts\test_source_boundary.py
.venv\Scripts\python scripts\test_mcp_server.py
.venv\Scripts\python scripts\verify_silent.py
.venv\Scripts\python scripts\ui_screenshots.py
.venv\Scripts\python scripts\privacy_scan.py
```

全部离线（临时数据目录 + 注入桩，注册表/进程/网络调用均被替换，未声明的
系统写入即测试失败）；`test_mcp_server` 会真实拉起本仓库的 MCP 服务器做
协议冒烟（只读诊断 + 指引语义断言）。

## 许可

MIT（见 LICENSE）。第三方依赖（PySide6、mcp 等）按其各自许可证分发。
