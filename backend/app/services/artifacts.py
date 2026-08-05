from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class ArtifactStoreError(ValueError):
    pass


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, requirement_id: str, kind: str, content: bytes) -> tuple[str, str, int]:
        requirement = _safe(requirement_id)
        safe_kind = _safe(kind)
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(requirement) / f"{safe_kind}-{digest[:16]}.bin"
        destination = (self.root / relative).resolve()
        if not destination.is_relative_to(self.root):
            raise ArtifactStoreError("artifact path escapes root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        return relative.as_posix(), digest, len(content)

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ArtifactStoreError("artifact does not exist")
        return path


def _safe(value: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value):
        raise ArtifactStoreError("artifact path component is invalid")
    return value
