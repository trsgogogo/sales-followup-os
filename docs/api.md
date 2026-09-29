# API 与状态规则

所有 `/api/*` 请求必须带 `Authorization: Bearer <FOLLOWUP_API_TOKEN>`。POST 使用 `Content-Type: application/json`，请求体最大 64 KiB。Token 不放在 URL。时间字段使用 UTC Unix 秒；不要传毫秒。不存在记录返回 404，输入错误 400，未认证 401。

## 线索入口

`POST /api/leads`

```json
{"name":"Example Buyer","company":"Example Co","email":"buyer@example.com","source":"website","external_id":"website:submission-123","notes":"客户希望了解服务范围，预算待确认。"}
```

返回完整线索记录，包含 `id` 和自动分配的 `owner`。`external_id` 用“来源:来源记录ID”格式；重复调用返回首次创建的线索，不会覆盖客户信息或重新分配负责人。不同来源同一邮箱不会自动合并，避免误合并同公司多人；导入方应先确定业务主键。

`GET /api/state` 返回客户、最近 500 个任务和 500 条通知、最近扫描时间。适用于小团队，尚未实现分页。

`GET /api/leads/{id}` 返回客户和最近 12 条事件。

## 客户事件

`POST /api/leads/{id}/events`

```json
{"kind":"reply","text":"客户询问实施周期，仍需确认预算。","idempotency_key":"mail:message-123"}
```

| kind | 额外字段 | 语义 |
| --- | --- | --- |
| `contacted` | `text` 可选 | 记录已经联系客户，取消当前待办；若仍在报价阶段，后续报价档位仍有效 |
| `reply` | 客户回复摘要 | 取消现有追踪、报价序列和下一步时间；等待负责人重新安排 |
| `quote` | 报价说明 | 记录新报价，清空旧跟进并从当前时间重启 3/7/14 天序列 |
| `meeting` | 会议结论 | 记录会议结束；若 24 小时内没有下一步则提醒 |
| `next_step` | `due`，未来时间 | 取代自动报价 / 会后追踪，按明确的下一步时间提醒 |
| `snooze` | `due`，未来时间 | 暂停当前提醒，到期后按当前阶段重新提醒 |
| `won` / `lost` | 说明可选 | 关闭商机并取消待发任务 |
| `optout` | 说明可选 | 取消全部追踪并永久阻止本应用重新开启 |
| `resume` | 说明可选 | 重新开启已成交 / 流失客户或结束延期，进入 contacted；需再设置下一步 |
| `assign` | `owner` | 更换负责人，撤销旧批准 |
| `note` | `text` | 补充事实，撤销旧批准 |

事件 `idempotency_key` 全局唯一。相同 key、客户、类型和内容重复提交不会重复写入；不同内容复用 key 返回 400。事件时间采用服务端接收时间，延迟同步不会追溯原始发生时间。录入历史资料时应明确选择下一步时间。

## 草稿与发送

`POST /api/leads/{id}/draft`，空 JSON `{}`。返回 `summary`、`draft`、`mode`（`ai` 或 `template`）及客户 `version`。不会自动发送或自动批准。

`POST /api/tasks/{id}/actions`：

```json
{"action":"approve","draft":"经销售确认的邮件正文。"}
```

- `approve`：保存人工确认的正文和当前客户版本。
- `send`：仅对当前已批准版本执行 SMTP 发送；还需启用 SMTP 配置。连续重复请求不会重复发送。
- `complete`：完成任务；不代表已经联系客户，不会自动改变商机阶段。实际联系需记录 `contacted`。
- `retry_notification`：将该任务失败的内部通知重新入队；不重发邮件。

SMTP 发送中断或结果不明确：标记 `delivery_unknown`，先查邮件服务商发送记录。已送达则标记任务完成；确需重新联系时创建明确下一步，避免盲目重试。

## 扫描与通知

`POST /api/tick`，空 JSON `{}`：检查到期任务并尝试投递通知。内置扫描每 30 秒运行；外部重复触发不会重复创建同一条任务。

通用通知 Webhook：

```json
{"event_id":"unique-notification-id","task_id":"task-id","lead_id":"lead-id","owner":"sales-a","text":"销售跟进提醒…"}
```

请求头同时含 `Idempotency-Key: <event_id>`。接收方应持久化去重。Webhook 不接受重定向；凭据由服务端配置固定，不允许线索字段指定发送地址。

`GET /healthz`：无需认证，仅暴露扫描是否新鲜。超过 180 秒未成功扫描返回 503；它不代表外部通知供应商可用。
