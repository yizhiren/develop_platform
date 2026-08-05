from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "ForgeFlow"
    app_env: str = "development"
    app_secret: str = "development-only-change-me-32-chars"
    database_path: Path = Path("data/forgeflow.db")
    redis_url: str = "redis://localhost:6379/0"
    artifact_root: Path = Path("artifacts")
    artifact_inline_max_bytes: int = 120_000
    web_origin: str = "http://localhost:3000"

    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "change-this-password"

    llm_provider: str = "fake"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    deepseek_api_key: str = Field(default="", repr=False)
    deepseek_api_key_file: Path | None = None
    run_live_ai_tests: bool = False
    live_ai_max_requests: int = 20

    github_app_id: str = ""
    github_token: str = Field(default="", repr=False)
    github_private_key: str = Field(default="", repr=False)
    github_webhook_secret: str = Field(default="", repr=False)
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_client_id: str = ""
    gitlab_client_secret: str = Field(default="", repr=False)
    gitlab_token: str = Field(default="", repr=False)
    gitlab_webhook_secret: str = Field(default="", repr=False)

    task_lease_seconds: int = 300
    task_max_attempts: int = 3
    task_retry_base_seconds: int = 2
    max_parallel_requirements: int = 2
    repository_automation_enabled: bool = False
    workspace_root: Path = Path("/workspaces")
    allow_local_git: bool = False
    workspace_max_bytes: int = 10 * 1024**3
    workspace_ttl_hours: int = 72
    sandbox_executor_socket: Path | None = None

    @property
    def database_url(self) -> str:
        path = self.database_path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
