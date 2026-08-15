"""只读机制健康度诊断兼容入口。"""
from __future__ import annotations

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.memory.evaluation import (  # noqa: E402,F401
    format_diagnosis_text_report as format_text_report,
    run_diagnosis,
)
from app.memory.evaluation_cli import diagnosis_cli_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return diagnosis_cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
