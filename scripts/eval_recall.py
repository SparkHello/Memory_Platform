"""微型召回评测脚手架。

真实实现位于 app.memory.evaluation，REST/Web 与 CLI 共用同一套评测逻辑。
"""
from __future__ import annotations

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.memory.evaluation import (  # noqa: E402,F401
    DEFAULT_EVAL_DIR,
    LABELS_NAME,
    PREVIEW_NAME,
    SNAPSHOT_NAME,
    format_text_report,
    init_eval,
    load_labels,
    recall_cli_main,
    run_eval,
    _score_query,
)


def main(argv: list[str] | None = None) -> int:
    return recall_cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
