# API quick reference

Base URL is supplied by the operator. Use `Authorization: Bearer <token>` from the authorized secret store, never from customer data. Do not print tokens.

- `GET /api/state`: leads, tasks, notifications, last_tick.
- `GET /api/leads/{id}`: current lead and recent events.
- `POST /api/leads`: `name`, optional `company`, `email`, `source`, `external_id`, `owner`, `notes`.
- `POST /api/leads/{id}/events`: `kind`, `text`, `idempotency_key`; `due` for next_step/snooze; `owner` for assign.
- `POST /api/leads/{id}/draft`: `{}`; returns summary, draft, mode, version.
- `POST /api/tasks/{id}/actions`: `action` = approve / send / complete / retry_notification. approve accepts edited `draft`.
- `POST /api/tick`: `{}`; deterministic scan, not a customer send.

Valid business events: contacted, reply, quote, meeting, next_step, won, lost, optout, snooze, resume, assign, note. due uses UTC Unix seconds, not milliseconds. Events use receipt time on the server. Reusing an idempotency key with a different event returns 400.

Complete marks a task done; it does not assert that a customer was contacted. send requires enabled SMTP, an approved current version, and an eligible customer. Customer context changes revoke approval. Reminder notifications can be pending, sent, failed or cancelled. Email can be delivery_unknown; do not retry without verifying the provider.
