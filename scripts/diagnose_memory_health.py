"""只读机制健康度诊断。

真实实现位于 app.memory.evaluation，REST/Web 与 CLI 共用同一套诊断逻辑。
"""
from __future__ import annotations

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.memory.evaluation import (  # noqa: E402,F401
    diagnosis_cli_main,
    format_diagnosis_text_report as format_text_report,
    run_diagnosis,
)


def main(argv: list[str] | None = None) -> int:
    return diagnosis_cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
