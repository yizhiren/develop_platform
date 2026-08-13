from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..agents.roles import AGENT_KEYS


@dataclass(frozen=True)
class AgentModelConfig:
    agent_key: str
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    api_key_file: Path | None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "画板"
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

    agent1_llm_provider: str = ""
    agent1_llm_base_url: str = ""
    agent1_llm_model: str = ""
    agent1_llm_api_key: str = Field(default="", repr=False)
    agent1_llm_api_key_file: Path | None = None

    agent2_llm_provider: str = ""
    agent2_llm_base_url: str = ""
    agent2_llm_model: str = ""
    agent2_llm_api_key: str = Field(default="", repr=False)
    agent2_llm_api_key_file: Path | None = None

    agent3_llm_provider: str = ""
    agent3_llm_base_url: str = ""
    agent3_llm_model: str = ""
    agent3_llm_api_key: str = Field(default="", repr=False)
    agent3_llm_api_key_file: Path | None = None

    agent4_llm_provider: str = ""
    agent4_llm_base_url: str = ""
    agent4_llm_model: str = ""
    agent4_llm_api_key: str = Field(default="", repr=False)
    agent4_llm_api_key_file: Path | None = None
    run_live_ai_tests: bool = False
    live_ai_max_requests: int = 20

    github_app_id: str = ""
    github_token: str = Field(default="", repr=False)
    github_api_enabled: str = ""
    github_private_key: str = Field(default="", repr=False)
    github_webhook_secret: str = Field(default="", repr=False)
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_client_id: str = ""
    gitlab_client_secret: str = Field(default="", repr=False)
    gitlab_token: str = Field(default="", repr=False)
    gitlab_api_enabled: str = ""
    gitlab_webhook_secret: str = Field(default="", repr=False)
    provider_secret_root: Path = Path("/provider-secrets")

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
    dependency_cache_root: Path = Path("/dependency-cache")
    dependency_install_timeout_seconds: int = 900
    dependency_install_scripts: bool = False

    @property
    def database_url(self) -> str:
        path = self.database_path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"

    def agent_model_config(self, agent_key: str) -> AgentModelConfig:
        if agent_key not in AGENT_KEYS:
            raise ValueError(f"unsupported agent key: {agent_key}")
        prefix = f"{agent_key}_llm_"
        provider = getattr(self, f"{prefix}provider") or self.llm_provider
        role_api_key = getattr(self, f"{prefix}api_key")
        role_api_key_file = getattr(self, f"{prefix}api_key_file")
        return AgentModelConfig(
            agent_key=agent_key,
            provider=provider,
            base_url=getattr(self, f"{prefix}base_url") or self.llm_base_url,
            model=getattr(self, f"{prefix}model") or self.llm_model,
            # The shared key is explicitly a DeepSeek credential. Never forward
            # it to an OpenAI endpoint when a role-specific key is missing.
            api_key=role_api_key or (self.deepseek_api_key if provider == "deepseek" else ""),
            api_key_file=role_api_key_file or (
                self.deepseek_api_key_file if provider == "deepseek" else None
            ),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
