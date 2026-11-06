# Lark 报销票据重命名助手

运行在本机的企业自建应用机器人。用户在 Lark 单聊发送图片或 PDF，程序逐个识别日期、金额、事项、币种，按新文件名将原文件回复到原消息。使用官方 SDK 长连接收事件，HTTP API 上传、回传文件。

## 启动与停止（macOS）

首次安装（Python 3.11）：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # 仅首次创建，已有 .env 时不要覆盖
chmod 600 .env
```

在 `.env` 填好 Lark 和模型凭证后运行：

```bash
./start.sh
curl http://127.0.0.1:8765/health
./stop.sh
```

启动脚本先验证配置与 Lark 鉴权，再通过当前用户的 launchd 在后台运行，关闭终端后继续工作，进程异常退出会自动重启。只有后台线程与 Lark 长连接均正常才报告启动成功。配置修改后先停止再启动。此方式不设置登录自动启动；重启电脑后需再次运行 `./start.sh`，电脑休眠时服务无法接收消息。

查看状态：`.venv/bin/python scripts/service.py status`。日志位于 `logs/bot.log`、`logs/stdout.log` 和 `logs/stderr.log`。

## 启动与停止（Windows）

需要 Python 3.11。项目已有独立 `.venv` 环境。

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
Invoke-RestMethod http://127.0.0.1:8765/health
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

`lark_connected: true` 表示长连接已建立；`worker_alive: true` 表示后台任务线程已运行。服务仅监听本机 127.0.0.1，日志在 `logs/bot.log`。电脑休眠、关机或进程退出后机器人无法继续工作；此项目未设置开机自动启动。

## 配置

实际凭证放在 `.env`，该文件已加入 Git 忽略。保留你自己的真实值，变量名应为：

```dotenv
LARK_APP_ID=cli_your_app
LARK_APP_SECRET=your_secret
LARK_DOMAIN=lark
LLM_API_HOST=https://your-model-provider/v1
LLM_API_KEY=your_model_key
LLM_MODEL=your_vision_model
```

国内飞书设置 `LARK_DOMAIN=feishu`。模型使用兼容 Chat Completions 的接口，必须支持 `image_url` 中的 base64 图片。票据内容会发送给 `.env` 中配置的模型服务。回调 Verification Token 不用于长连接鉴权。

可选：`LLM_TIMEOUT_SECONDS=180`、`MAX_FILE_MB=25`、`MAX_PDF_PAGES=20`、`FILE_RETENTION_HOURS=24`、`HEALTH_PORT=8765`。改配置后需要重启服务。

支持 `LLM_MODEL_BACKUP` 备用视觉模型；主模型接口失败时自动尝试备用模型。`LLM_API_HOST_BACKUP` 和 `LLM_API_KEY_BACKUP` 未填时复用主接口和 Key。当前主模型直接使用已通过图片识别和真实聊天回传验证的 `x-ai/grok-4.1-fast`，备用模型暂留空。

## Lark 开发者后台

1. 开启企业自建应用的机器人能力。
2. 开通应用身份权限：`im:message.p2p_msg:readonly`、`im:message:readonly`、`im:resource`、`im:message:send_as_bot`。
3. 在本机服务建立长连接后，在「事件与回调 → 事件配置」选择长连接并订阅 **接收消息** `im.message.receive_v1`。
4. 创建版本并发布，确保使用者在应用可用范围内。
5. 如果需要群里使用，额外申请 `im:message.group_at_msg:readonly`，将机器人加入群聊。群聊事件是否推送按 Lark 的消息与 @ 规则决定，建议首版使用单聊。

## 多文件处理规则

- 连续上传的每个附件独立持久化排队，按到达顺序串行处理，不会并发无限调用模型。
- 同一富文本消息内的多张图片分别处理。不同用户、不同附件使用独立目录，每个文件发起独立模型请求，避免上下文串用。
- 每个结果回复对应的原消息。默认不打 ZIP，不合并用户连续发来的不同文件。
- 使用消息 ID + 资源 Key 去重。重启后恢复未完成任务，已保存的识别结果和上传文件 Key 会复用。发送回复使用稳定 UUID；飞书 API 的 UUID 去重有效期为 1 小时，因此极端情况下跨小时的崩溃重放仍可能产生重复回复。
- 某个文件失败会单独通知，其余文件继续处理。在原会话发送 **重试** 可重试当前用户最近一个失败附件，也可重新发送该文件。
- 单张图片或单份 PDF 含多张票据时逐张提取，并在回复中列出每张的日期和金额。同币种加总，混合币种分别合计。票据未明确标注币种时，按该张票据的开票地点或商户地址推断。回传保留完整原文件，跨页的同一张票据只计一次。

## 命名与文件保留

单张票据示例：`2026-11-03_住宿费_368.00_CNY.pdf`。发票取开票日期、价税合计；收据取交易日期、实付总额。字段不清时文件名使用「日期待确认」「金额待确认」等标记，并发送核对提示。文件名由程序校验和生成，保留原始扩展名对应的格式。

识别时会渲染图片/PDF 页面，但回传的是下载后的原始文件字节，只修改文件名，不重写 PDF、不改发票签名。通过聊天图片发送的图片，源字节可能已由 Lark 客户端压缩；如需保持用户电脑上的源文件字节，请以文件形式上传图片。

任务状态在 `data/jobs.sqlite3`；附件和识别结果在 `data/work/`。完成或失败任务在配置的保留期限后清理（默认 24 小时，服务运行时约每 10 分钟检查一次）。文件过期后需重新上传；日志不记录密钥或票据正文。

限制：单文件默认 25 MB，PDF 最多 20 页；超限明确报错，不会静默只读前几页。加密/损坏 PDF、多帧图片、其他文件格式会单独提示。

## 验证与本地处理

```powershell
.\.venv\Scripts\python.exe -m receipt_bot.main check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m receipt_bot.main process "C:\path\invoice.pdf"
```

最后一条直接验证真实模型识别和重命名，不向任何聊天发消息。输出在 `data/local-output/`。

参考：[官方长连接 SDK](https://github.com/larksuite/oapi-sdk-python)、[官方上传并发送文件示例](https://github.com/larksuite/oapi-sdk-python-demo/blob/main/composite_api/im/send_file.py)。
