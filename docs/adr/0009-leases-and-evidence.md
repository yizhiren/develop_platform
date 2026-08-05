# ADR-0009：分布式需求租约与内容寻址证据

- 状态：接受
- 日期：2026-08-05

## 背景

多个 Agent/Git Worker 横向扩展后，单进程串行无法保证全局并发上限，也不能防止同一需求同时执行互相冲突的阶段。大型 Diff 全量存入 SQLite 会放大数据库与备份，而只保存截断文本又无法审计真实交付。

## 决策

- 使用 Redis task lock 防重复任务、requirement lock 防同需求并行、按过期时间排序的全局 ZSET 控制活动需求数。
- Lua 脚本原子清理过期成员、检查上限并取得需求锁；Worker 每个租约周期的三分之一发送心跳，退出后由 TTL 自动释放。
- 默认 `MAX_PARALLEL_REQUIREMENTS=2`，所有 Worker 实例必须配置相同值。
- 大于 `ARTIFACT_INLINE_MAX_BYTES` 的交付 Diff 以 SHA-256 为文件名写入 Artifact Volume；相同内容复用同一对象。
- SQLite `evidence` 保存需求归属、类型、相对路径、SHA-256 和字节数；下载前执行项目 RBAC 与路径解析校验。SQLite 仅保留限额预览和完整证据引用。

## 后果

平台可以安全横向扩展 Worker，并以明确容量保护 SQLite 和外部模型配额；进程崩溃不会永久占用槽位。完整大型证据可验证且不膨胀 SQLite。Redis 暂时不可用时新任务不会执行，但权威状态仍在 SQLite，Redis 恢复后可重建；Artifact Volume 需要和 SQLite 备份建立一致的保留策略。
