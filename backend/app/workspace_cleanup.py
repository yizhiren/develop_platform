from __future__ import annotations

import json

from .core.config import get_settings
from .services.git_workspace import GitWorkspaceManager


def main() -> None:
    manager = GitWorkspaceManager(get_settings())
    removed = manager.cleanup_stale()
    print(json.dumps({"removed_count": len(removed), "requirement_ids": removed}))


if __name__ == "__main__":
    main()
