from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class GitProviderError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
    state: str


class GitProvider(ABC):
    """Provider contract implemented by GitHub and GitLab adapters.

    Implementations run only in the trusted Git Worker. Agent sandboxes never receive
    an instance of this interface or its credentials.
    """

    @abstractmethod
    async def get_repository(self, external_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def create_branch(self, repository: str, branch: str, base_sha: str) -> str: ...

    @abstractmethod
    async def create_or_update_pull_request(
        self, repository: str, head: str, base: str, title: str, body: str
    ) -> PullRequestRef: ...

    @abstractmethod
    async def get_checks(self, repository: str, sha: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def merge(self, repository: str, number: int, expected_head_sha: str) -> str: ...

    @abstractmethod
    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...
