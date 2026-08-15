"""Offline CLI for the authoritative portable stack-backup validator."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from app.stack_backup import validate_stack_backup


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise ValueError("expected exactly one portable backup path")
    result = validate_stack_backup(archive_path=Path(arguments[0]))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # A malformed archive may contain attacker-controlled names. Keep the
        # installer diagnostic useful without reflecting archive contents.
        print(f"backup verification failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
