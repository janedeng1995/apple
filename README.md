# AppleMonitorPro 分享包

这是一个已经脱敏的 Windows 分享包。默认配置是“示例账号 + 示例取货人”，两者都处于禁用状态，并且程序保持演练模式，不会下单。

完整说明书位于 `使用说明书\使用说明书.docx`，同时提供 `使用说明书.pdf` 和 `使用说明书.md`。源码有修改后，必须在 Windows 上重新构建 EXE 才能让二进制程序包含新逻辑。

## 一分钟开始

1. 保持整个文件夹完整，不要只单独复制 `AppleMonitorPro.exe`。
2. 在 PC 微信中登录要抢购的账号，并打开一次苹果小程序购物袋页面。
3. 回到分享包，双击 `工具\一键抓Cookie.cmd`，在 UAC 提示中点“是”。
4. 看到 `HTTP 200` 和 `✓ 已存 身份-PC-xxxx.txt`，说明 Cookie 抓取成功。
5. 双击 `AppleMonitorPro.exe`。
6. 打开“抢购任务”页：
   - 选择刚生成的 `身份-PC-xxxx.txt`；
   - 选择要抢的型号；
   - 选择该任务唯一绑定的取货人；
   - 设置每单件数和任务累计上限；
   - 勾选“启用”；
   - 一个身份文件只能用于一个任务。
7. 打开“取货人”页，填写真实姓名、手机号、邮箱、证件后四位，并选择型号和城市。
   - 每个任务只绑定一个取货人；
   - 多个账号/任务可以共用同一取货人。
8. 先使用“演练”模式启动，确认能看到扫描与命中流程，但不会建单。
9. 确认配置无误后，再切换到“真实下单”模式，并阅读程序弹出的确认提示。

不会操作时，直接把 `工具\给豆包-抓Cookie教程.md` 发给豆包，让豆包照着教你。

## 身份文件怎么填

在 `身份` 文件夹中新建一个 `.txt` 文件，内容格式如下：

```json
{
  "cookie": "这里粘贴完整 Cookie",
  "ua": "这里可选粘贴微信 User-Agent"
}
```

然后在 `config.json` 的对应任务中填写相对路径，例如：

```json
{
  "name": "我的账号",
  "cookie_file": "身份/我的账号.txt",
  "sku": "MJTL4CH/A",
  "buyer": "张三",
  "per_order": 1,
  "want": 1,
  "enabled": true
}
```

注意：

- 身份文件等同于账号登录凭据，不能公开分享。
- 一个身份文件不能同时配置给两个任务，否则购物袋会互相覆盖。
- 包内 `身份/示例-请替换.txt` 只是空模板，没有真实凭据。
- 最简单的方式：双击 `工具\一键抓Cookie.cmd`，工具会自动生成身份文件到 `身份` 文件夹。
- 不需要自己运行 Python；分享包已包含 `工具\抓Cookie-PC.exe`。
- 使用说明见 `工具\抓Cookie-先看我.txt`；给豆包的教程见 `工具\给豆包-抓Cookie教程.md`。

## 配置重点

`config.json` 的核心字段：

| 字段 | 说明 |
|---|---|
| `cookies[].name` | 任务名称，只用于显示 |
| `cookies[].cookie_file` | 身份文件路径，推荐使用 `身份/文件名.txt` 相对路径 |
| `cookies[].sku` | 要抢的具体型号代码，例如 `MJTL4CH/A` |
| `cookies[].buyer` | 该任务唯一绑定的取货人；同一人可被多个任务复用 |
| `cookies[].store_source` | 门店来源（任务卡上二选一）：`buyer` = 按取货人意向地址（城市）扫店（默认）；`store` = 按任务自定义门店 |
| `cookies[].stores` | `store_source="store"` 时要盯的门店编号列表，可多选。会话预热在占位店（不建在目标店上），命中时切到目标店拿当剏 spokes（这样才拉得到时段）；本次未成会话会退回占位店 |
| `cookies[].per_order` | 单次订单件数，会自动受任务和取货人剩余额度限制 |
| `cookies[].want` | 这个账号任务的累计上限 |
| `cookies[].enabled` | 是否启用这个任务 |
| `buyers[].name` | 取货人姓名 |
| `buyers[].phone` | 11 位手机号 |
| `buyers[].email` | 接收订单通知的邮箱 |
| `buyers[].last4` | 证件后四位，必须是 4 位字母或数字 |
| `buyers[].skus` | 该取货人接受的型号列表；留空表示不限型号 |
| `buyers[].cities` | 该取货人可用的城市列表 |
| `buyers[].want` | 该取货人在所有共享账号下的合计上限 |
| `buyers[].enabled` | 是否启用这个取货人 |
| `settings.dry_run` | `true` 为演练模式，不提交订单 |
| `settings.auto_submit` | `true` 为真实下单，请谨慎开启 |
| `settings.prewarm_contact` | 是否在预热阶段写入绑定取货人；关闭后命中门店再现场填入 |
| `settings.require_time_slot` | 【需要时段】开关。`false`（默认）=没有可填时段也补完其它根照常提交；`true`=发现没有填写时段的地方就当没货，不进结算、不提交 |
| `settings.store_cool_sec` | 门店进结算失败（含没时段、门店被抚）后的冷却秒数，默认 15。每一轮间隔固定一样（不指数增长），设 0 = 不冷却。只影响进结算，探测照旧 |
| `settings.time_slot_index` | 默认日期内选择第几个可用时段，从 `1` 开始；超出数量时取最后一个 |
| `settings.prewarm_interval_sec` | 启动时账号之间的统一预热间隔；例如 `5` 表示每 5 秒启动一个账号预热 |

门店来源在任务卡上切换：【取货人意向地址】按取货人填的城市展开扫店（默认，和以前一致）；【门店自定义】只扫任务上勾选的几家（可多选，不受取货人城市限制）。

时段是服务端状态，不再由本地开关提前结束：有可选时段就先保存；没有时段也会尽力补齐其他信息并提交。如果提交弹回仍是空时段，会补完其他格后再提交一次；提交受理后的状态查询遇到空时段也会继续轮询，以 Apple 最终响应为准。

运行中保存取货人、SKU、数量、城市和运行参数后，worker 会在下一轮按稳定任务 ID 换成新配置，并作废不再匹配的旧结算会话。增删任务、更换身份文件或代理设置后，需停止再重新开始。

代理模式下，程序不再改写提取链接里的 IP 数量。每个池只消费服务商返回的第一个 IP，快过期或连接失败时异步换新，取不到时当前请求直连。

型号名称和完整 SKU 映射在 `src/apple_monitor/config.py` 的 `SKU_NAMES` 中。

## 文件说明

| 文件或目录 | 是否必需 | 说明 |
|---|---|---|
| `AppleMonitorPro.exe` | 是 | 主程序 |
| `config.json` | 是 | 当前运行配置，已脱敏 |
| `config.example.json` | 是 | 同配置模板 |
| `资料-城市门店对照.json` | 是 | 城市与门店编号对照 |
| `资料-门店中文名.json` | 是 | 门店编号与中文名对照 |
| `身份/` | 是 | 每个账号一个身份文件；抓 Cookie 工具会把结果放在这里 |
| `工具/一键抓Cookie.cmd` | 是 | 推荐入口；自动请求管理员权限并抓取 Cookie |
| `工具/抓Cookie-PC.exe` | 是 | 自带运行环境的单文件抓 Cookie 程序 |
| `工具/给豆包-抓Cookie教程.md` | 建议保留 | 可直接发给豆包照做的图文步骤 |
| `使用说明书/使用说明书.docx` | 建议保留 | 可直接发给普通用户阅读的 Word 使用说明书 |
| `使用说明书/使用说明书.pdf` | 建议保留 | PDF 版说明书 |
| `使用说明书/使用说明书.md` | 建议保留 | 方便 AI 阅读和继续修改的说明书源文件 |
| `orders-ledger.json` | 建议保留 | 已抢数量账本；发布包中是空白账本 |
| `src/` | 可选 | 源码，便于 AI 理解或重新打包 |
| `AI-README.md` | 可选 | 给 AI 阅读的项目说明 |
| `发布前检查.ps1` | 可选 | 发送前检查是否混入凭据或个人数据 |

程序运行后会自动创建：

- `logs/`：运行日志；
- `sessions/`：热会话及调试数据。
- `orders.json`：订单号与访客链接；
- `支付二维码/`：支付二维码图片。

## 重新构建 exe

需要 Windows、Python 3，以及可联网安装依赖：

```powershell
cd src
python -m pip install -r requirements.txt
python -m pip install -U pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name AppleMonitorPro --add-data "config.example.json;." --collect-submodules apple_monitor --collect-all curl_cffi --collect-all ttkbootstrap run.py
```

构建出的程序位于 `src/dist/AppleMonitorPro.exe`。发布时把该 exe 复制到包根目录，并确认 `config.example.json` 已经脱敏。

也可以直接运行源码版：

```powershell
src\启动源码版.cmd
```

## 安全说明

使用过真实配置后，以下内容会变成敏感文件，不能再次散播：

- `config.json`
- `身份/*.txt`
- `orders-ledger.json`
- `logs/`
- `sessions/`
- `orders.json`
- `支付二维码/`
- 任何含真实信息的 `config.share.json` / `config.local*.json`

发送给别人前，请先运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\发布前检查.ps1
```

检查脚本不会显示完整敏感值，只会指出风险文件。

## 常见问题

- **提示找不到配置文件**：确认 `AppleMonitorPro.exe` 与 `config.json` 在同一层。
- **提示身份文件不存在**：确认 `cookie_file` 路径和实际文件名一致，推荐用相对路径。
- **配置验证失败**：手机号必须是 11 位数字；证件后四位必须是 4 位字母或数字；城市必须能在门店资料中找到。
- **不想真实下单**：保持 `settings.dry_run = true`、`settings.auto_submit = false`。
- **程序无法启动**：先确认系统为 Windows 10/11，并保持整个分享包目录结构不变。
