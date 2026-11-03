# AI-README：AppleMonitorPro 分享包

本文件用于让代码助手快速理解此目录，并在不泄露账号凭据的前提下协助配置、排错或修改程序。

## 安全边界

1. `config*.json`、`身份/*.txt`、`orders-ledger.json`、`orders.json`、`logs/`、`sessions/`、`支付二维码/` 可能包含个人数据或账号凭据。
2. 默认必须保持 `settings.dry_run = true` 和 `settings.auto_submit = false`。
3. 未经用户明确、重复确认，不得把 `auto_submit` 改为 `true`，不得把 `dry_run` 改为 `false`。
4. 不要输出、总结或回显完整 Cookie、邮箱、手机号、证件后四位或微信账号指纹。排错时只输出字段名、长度、哈希或脱敏片段。
5. 修改前先检查 `发布前检查.ps1` 和 `.gitignore`，不要把运行后产生的敏感文件纳入压缩包或 Git。

## 目录结构

```text
AppleMonitorPro-分享包/
├─ AppleMonitorPro.exe          Windows 单文件程序
├─ config.json                  当前配置（已脱敏的禁用示例）
├─ config.example.json          配置模板
├─ orders-ledger.json           空白抢购额度账本
├─ 资料-城市门店对照.json        城市 -> Apple 门店编号
├─ 资料-门店中文名.json          门店编号 -> 中文显示名
├─ 身份/
│  ├─ 示例-请替换.txt            空身份模板
│  ├─ README-身份文件格式.txt
│  └─ 身份-PC-xxxx.txt          抓 Cookie 工具生成的真实身份文件
├─ 工具/
│  ├─ 一键抓Cookie.cmd          用户双击入口，自动请求管理员权限
│  ├─ 抓Cookie-PC.exe           自带 curl_cffi 的单文件抓取程序
│  ├─ 抓身份-PC.py              Python 源码版本
│  ├─ wx_cookie.py              微信进程内存扫描依赖
│  ├─ 抓Cookie-先看我.txt       普通用户说明
│  └─ 给豆包-抓Cookie教程.md    可直接发送给豆包的教程
├─ src/
│  ├─ run.py                     Python 入口
│  ├─ requirements.txt           源码运行依赖
│  ├─ build_exe.bat              PyInstaller 一键构建
│  ├─ AppleMonitorPro.spec
│  └─ apple_monitor/
│     ├─ config.py              配置解析、门店映射、取货人选择
│     ├─ client.py              HTTP 会话、Cookie、限流退避
│     ├─ session.py             结算流程
│     ├─ scan.py                库存扫描
│     ├─ worker.py              单账号工作线程
│     ├─ notify.py              响铃和邮件
│     ├─ quota.py               额度账本
│     ├─ gui.py                 Tkinter/ttkbootstrap 界面
│     └─ main.py                 GUI/命令行入口
├─ README.md                    给人看的使用步骤
├─ 使用说明书/                 完整说明书（DOCX / PDF / Markdown）
└─ 发布前检查.ps1                发送前敏感信息检查
```

## 运行方式

EXE：

```powershell
.\AppleMonitorPro.exe
```

源码 GUI：

```powershell
.\src\启动源码版.cmd
```

源码命令行演练：

```powershell
$env:APPLE_HOME = (Resolve-Path .).Path
python .\src\run.py --headless --dry
```

`APPLE_HOME` 很重要：源码运行时，它让程序把包根目录作为数据目录，从根目录读取 `config.json`、身份文件和门店资料。

## 抓 Cookie 工作流

用户端标准入口：

```text
工具\一键抓Cookie.cmd
```

程序实现：

```text
工具\抓Cookie-PC.exe
工具\抓身份-PC.py
工具\wx_cookie.py
```

前置条件：

1. Windows PC 微信已登录目标账号。
2. 已在 PC 微信中打开苹果小程序并进入购物袋页面。
3. 通过 `一键抓Cookie.cmd` 启动，使程序获得管理员权限。

输出：

```text
身份\身份-PC-xxxxxxxx.txt
```

该文件是 JSON，至少包含：

```json
{
  "cookie": "真实 Cookie，不能公开",
  "ua": "PC 微信 User-Agent",
  "note": "抓取时间与验证结果"
}
```

成功标志是控制台出现 HTTP 200 和 `✓ 已存 身份-PC-xxxxxxxx.txt`。AI 排错时只能读取错误文字或 HTTP 状态码，不得要求用户粘贴完整 Cookie。豆包专用教程位于 `工具\给豆包-抓Cookie教程.md`。

## 配置模型

`config.json` 顶层结构：

```json
{
  "cookies": [],
  "buyers": [],
  "cities": {},
  "settings": {}
}
```

### cookie 任务

```json
{
  "name": "显示名称",
  "cookie_file": "身份/文件名.txt",
  "sku": "MJTL4CH/A",
  "enabled": false,
  "ua": ""
}
```

- `cookie_file` 相对包根目录解析。
- 一个 cookie 文件只能出现于一个启用任务。
- `ua` 留空时读取身份 JSON 的 `ua`；仍为空则使用程序默认值。
- 支持的 SKU 完整表位于 `src/apple_monitor/config.py` 的 `SKU_NAMES`。

### 取货人

```json
{
  "name": "姓名",
  "phone": "17000000000",
  "email": "example@example.com",
  "last4": "0000",
  "last_name": "姓",
  "first_name": "名",
  "want": 1,
  "skus": ["MJTL4CH/A"],
  "cities": ["北京"],
  "enabled": false
}
```

- `phone` 必须是 11 位数字。
- `last4` 必须是 4 位字母或数字。
- `skus` 为空表示不限制型号。
- `cities` 必须存在于门店资料中。
- `want` 是该取货人的总目标数量，额度由 `orders-ledger.json` 持久化。
- 每个 cookie 任务只绑定一个取货人，但多个任务可共用同一取货人。
- 账本同时限制 `cookies[].want` 任务额度和 `buyers[].want` 取货人合计额度，提交前会并发预占。

### 关键设置

- `dry_run`: 演练模式；默认 `true`。
- `auto_submit`: 真实自动提交；默认 `false`。
- `scan_concurrency`: 扫描并发。
- `scan_interval_sec`: 扫描间隔。
- `prewarm_interval_sec`: 多任务启动预热错峰。
- `session_rebuild_sec`: 热会话重建时间。
- `status_poll_sec` / `status_poll_max`: 提交后订单状态轮询节奏和次数上限。
- `retry_no_slot_sec`: 服务端最终明确拒绝后的同店冷却时间。
- 无时段不再提前结束：会继续补齐联系人/付款/发票并尝试 `placeOrder`；
  若提交弹回仍是空时段，补完其他格后再提交一次。提交受理后的 status
  轮询遇到空时段也不会立即结束，会继续查到感谢页或确认上限。
- 运行中保存配置会按稳定任务 ID 热更新取货人、SKU、数量、城市和运行参数；
  任一结算相关值变化都会作废旧预热会话。增删任务、更换身份文件或代理设置仍需停止后重新开始。
- 代理不改写 API 提取数，每池只持有一个当前 IP，失效时异步换新。
- `debug_checkout`: 是否保存结算调试数据；默认关闭，开启后会写敏感日志。
- `email`: SMTP 通知配置；密码字段必须保持空值或本地私密保存。

## 程序流程

```text
读取 config.json
  -> 校验启用任务、身份文件、取货人格式和城市门店
  -> 每个启用 cookie 启动一个 worker
  -> 预热会话
  -> 扫描目标城市的库存
  -> 命中后换店；有时段则保存，无时段也继续完整提交
  -> 成功/失败事件写入 logs 与 orders-ledger.json
```

`gui.py` 负责配置和展示；`worker.py` 是单账号调度中枢；`session.py` 是结算链路；`scan.py` 是库存探测。

## 开发检查清单

1. 修改代码后运行 Python 语法检查：

```powershell
python -m compileall .\src
```

然后运行无网络回归测试：

```powershell
$env:PYTHONPATH = ".\src"
python -m unittest discover -s .\tests -v
```

2. 检查配置解析：

```powershell
$env:APPLE_HOME = (Resolve-Path .).Path
python -c "from src.apple_monitor.config import Config; c=Config.load('config.json'); print('\n'.join(c.validate()) or 'OK')"
```

3. 运行演练模式，不要直接真实下单：

```powershell
$env:APPLE_HOME = (Resolve-Path .).Path
python .\src\run.py --headless --dry
```

4. 修改 `config.example.json` 后重新构建 exe，确保模板不再含个人信息。

5. 发送前运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\发布前检查.ps1
```

## 推荐给 AI 的任务描述

可以复制下面内容给 AI：

> 你正在维护 `AppleMonitorPro-分享包`。先阅读 `AI-README.md` 和 `README.md`，再检查 `src/apple_monitor/config.py`、`worker.py`、`session.py` 的调用关系。不要在输出中回显真实 Cookie、手机号、邮箱、证件信息或账号指纹；未经我明确确认，不得开启 `auto_submit` 或关闭 `dry_run`。修改后先做语法检查，并说明如何用演练模式验证。
