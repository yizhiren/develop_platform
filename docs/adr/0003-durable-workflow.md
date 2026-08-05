# ADR-0003：SQLite Outbox 与 Redis Streams

- 状态：接受
- 日期：2026-08-05

需求状态和任务保存在 SQLite，传输使用 Redis Streams。事务 Outbox、租约、心跳和幂等键提供崩溃恢复，必须验证 Redis 丢失、重复投递和 Worker 崩溃。
