from __future__ import annotations

import json
from pathlib import Path

from .backup import verify_restore


def main() -> None:
    backups = sorted(Path("/backups").glob("forgeflow-*.db"))
    if not backups:
        raise RuntimeError("no ForgeFlow backup was found")
    destination = Path("/tmp/forgeflow-restored.db")
    destination.unlink(missing_ok=True)
    result = verify_restore(backups[-1], destination)
    print(json.dumps({"backup": backups[-1].name, **result}))


if __name__ == "__main__":
    main()
