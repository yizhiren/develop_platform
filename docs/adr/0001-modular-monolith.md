# ADR-0001：模块化单体与独立 Worker

- 状态：接受
- 日期：2026-08-05

业务 API 和状态机采用一个 Control Plane，耗时的 Agent、Git 与沙箱操作使用独立 Worker。MVP 不拆分为大量微服务，以保持事务和权限一致性并降低部署成本。
