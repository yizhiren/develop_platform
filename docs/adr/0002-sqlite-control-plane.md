# ADR-0002：SQLite WAL 与控制平面独占访问

- 状态：接受
- 日期：2026-08-05

SQLite WAL 是 MVP 权威数据库，仅 Control Plane 可以打开数据库文件。默认并发两个需求，并通过 SQLAlchemy/Alembic 保留迁移 PostgreSQL 的路径。
