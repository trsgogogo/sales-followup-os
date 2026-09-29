# 集成指南

## n8n

仓库包含四个原创模板，默认关闭（`active: false`）。导入后将 HTTP 节点里的 `https://YOUR-FOLLOWUP-SERVER` 替换为实际部署地址，再设置凭据。模板不包含 Token。

1. 新建 **Header Auth** 凭据，Name=`Authorization`，Value=`Bearer <FOLLOWUP_API_TOKEN>`，用于调用 Follow-up API 的 HTTP 节点。
2. Webhook 入口使用另一份 Header Auth 凭据，用于验证表单 / CRM 发来的请求。不要将管理 Token 交给公共网页。
3. 导入 `01-lead-intake.json`：接受 API 文档中的线索字段，转发到 `/api/leads`，向调用方返回创建结果。
4. 导入 `02-customer-events.json`：输入 `lead_id`、`kind`、`text`、`idempotency_key`，必要时加 `due` 或 `owner`。邮箱真实回复映射为 `reply`；退订映射为 `optout`。
5. `03-scheduled-scan.json` 每分钟调用 `/api/tick`。应用自带 30 秒扫描，此模板是可选的外部触发入口；核心去重逻辑在应用里。
6. `04-internal-notification.json` 可将通用通知转发到 Slack Webhook。配置 `NOTIFY_WEBHOOK_TOKEN`，并使入口 Header Auth 的 Authorization 值匹配 `Bearer <NOTIFY_WEBHOOK_TOKEN>`。默认 Slack 占位地址必须替换；生产场景建议使用 n8n 凭据管理接收渠道地址。此模板为简单转发，不提供接收端持久化去重。
7. 使用合成客户跑通入口、回复停止和到期提醒，再发布工作流。计划触发器需要发布后才会定时执行。

若 n8n 与应用都在容器中，`localhost` 指向各自容器。使用同一 Docker 网络上的服务名，或 HTTPS 反向代理地址。凭据节点使用 n8n 支持的 Header Auth，不依赖环境变量表达式读取权限。

官方参考：[Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/)、[HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)。

## Activepieces

使用同样的 API，不需要另一套数据库。这里提供配置步骤，不宣称为可导入的 Activepieces JSON。

| Flow | Trigger | HTTP action |
| --- | --- | --- |
| 新线索录入 | 表单 / HubSpot 新联系人 / Webhook | POST `/api/leads` |
| 客户回复停止 | 邮箱真实入站回复 / CRM 对话事件 | POST `/api/leads/{id}/events`，kind=`reply` |
| 商机成交 | CRM 阶段变更 | 同上，kind=`won` |
| 外部扫描 | 每分钟 Schedule | POST `/api/tick` |

在连接配置里保存 Authorization Bearer Token；使用持久映射将 CRM contact/deal ID 对应到本应用 lead ID。处理邮件自动回复、退信和群发回执时，应先过滤，不要将其全部当成人工回复。将原邮件 message ID 作为幂等键。

## 内部通知

- Slack：`NOTIFY_FORMAT=slack`，URL 为 Incoming Webhook。
- 飞书：`NOTIFY_FORMAT=feishu`，使用自定义机器人 Webhook。当前适配文本格式；开启额外签名验证的机器人需要自行增加签名适配器。
- 企业微信：`NOTIFY_FORMAT=wecom`，使用群机器人 Webhook。适用于内部群提醒，不是个人微信读写。
- 通用 Webhook：`NOTIFY_FORMAT=generic`，便于 n8n / Activepieces 接收，包含 event_id 和负责人。

机器人响应中的 `code` / `errcode` 会检查。5 次失败后显示在工作台，修复地址 / 权限后点“重试失败通知”。

## 邮件与 AI

SMTP 使用 STARTTLS（通常 587），不支持此版本中的隐式 TLS 465。上线前用你控制的测试收件箱验证发件人和域名设置。应用不会建立原 Gmail 线程，也不会读取回复；回复入口由邮箱集成提供。

AI 通过可配置的 HTTPS Chat Completions 兼容地址接入。模型调用仅发生在手动请求草稿时，定时器不会持续消耗模型额度。客户文本仅作为数据输入，输出只读取 JSON 中的摘要与草稿，不具备工具调用或写入 CRM 的权限。
