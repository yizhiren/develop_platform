from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4


SUPPORTED_PROVIDERS = {"github", "gitlab"}


class ProviderSecretError(ValueError):
    pass


class ProviderSecretStore:
    """File-backed provider credentials shared only with trusted services."""

    def __init__(self, root: Path):
        self.root = root

    def configured(self, provider: str) -> bool:
        path = self._path(provider)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_size > 0

    def read(self, provider: str) -> str:
        path = self._path(provider)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return ""
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise ProviderSecretError("provider credential file is invalid")
        token = path.read_text(encoding="utf-8")
        self._validate_token(token)
        return token

    def write(self, provider: str, token: str) -> None:
        self._validate_token(token)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        destination = self._path(provider)
        temporary = self.root / f".{provider}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, provider: str) -> None:
        self._path(provider).unlink(missing_ok=True)

    def _path(self, provider: str) -> Path:
        if provider not in SUPPORTED_PROVIDERS:
            raise ProviderSecretError("unsupported provider")
        return self.root / f"{provider}.token"

    @staticmethod
    def _validate_token(token: str) -> None:
        if not 20 <= len(token) <= 4096:
            raise ProviderSecretError("provider token length is invalid")
        if token != token.strip() or any(ord(character) < 33 or ord(character) == 127 for character in token):
            raise ProviderSecretError("provider token contains whitespace or control characters")
