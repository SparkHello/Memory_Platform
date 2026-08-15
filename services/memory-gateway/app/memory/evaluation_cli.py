"""Thin command-line adapters for memory evaluation and diagnosis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from app.memory.evaluation import (
    DEFAULT_EVAL_DIR,
    MAX_RECALL_EVAL_K,
    EvaluationError,
    format_diagnosis_text_report,
    format_text_report,
    init_eval,
    run_diagnosis,
    run_recall_eval,
)
from app.memory.search import EmbeddingClient, NullEmbeddingClient


def _build_embedding_client() -> EmbeddingClient:
    from app.api.deps import get_embedding_client
    from app.config import get_settings

    return get_embedding_client(get_settings())


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def recall_cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Micro recall evaluation for memory-gateway search."
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Snapshot the real DB and scaffold labels.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the evaluation against the snapshot.",
    )
    parser.add_argument(
        "--database",
        default="data/memory.db",
        help="Real SQLite database path (read-only).",
    )
    parser.add_argument(
        "--eval-dir",
        default=DEFAULT_EVAL_DIR,
        help="Directory for snapshot/preview/labels.",
    )
    parser.add_argument(
        "--user-id",
        default="default",
        help="X-User-Id scope to evaluate.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=8,
        help=f"Top-k cutoff (1-{MAX_RECALL_EVAL_K}).",
    )
    parser.add_argument(
        "--use-embedding",
        action="store_true",
        help="Use the real embedding provider for queries.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    args = parser.parse_args(argv)

    _configure_stdout()
    if not args.init and not args.run:
        parser.error("Specify --init or --run.")

    eval_dir = Path(args.eval_dir)
    if args.init:
        result = init_eval(
            source_db=args.database,
            eval_dir=eval_dir,
            user_id=args.user_id,
        )
        if result.get("error"):
            print(result["error"])
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                f"Snapshot: {result['snapshot']} "
                f"({result['memory_count']} active memories)"
            )
            print(f"Preview:  {result['preview']}")
            labels_status = (
                "created template" if result["labels_created"] else "kept existing"
            )
            print(f"Labels:   {result['labels']} ({labels_status})")
            print(
                "User scopes: "
                + json.dumps(result["user_counts"], ensure_ascii=False)
            )
            print("\nNext: edit labels.jsonl to fill relevant_ids, then run --run.")
        if not args.run:
            return 0

    mode = "embedding" if args.use_embedding else "keyword"
    try:
        result = run_recall_eval(
            eval_dir=eval_dir,
            user_id=args.user_id,
            mode=mode,
            k=args.k,
            embedding_client=(
                _build_embedding_client()
                if args.use_embedding
                else NullEmbeddingClient()
            ),
        )
    except (FileNotFoundError, EvaluationError) as exc:
        print(str(exc))
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text_report(result))
    return 0


def diagnosis_cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only diagnosis of whether memory mechanisms are activated "
            "by real data."
        )
    )
    parser.add_argument(
        "--database",
        default="data/memory.db",
        help="SQLite database path.",
    )
    parser.add_argument("--user-id", default=None, help="Optional user scope.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    args = parser.parse_args(argv)

    _configure_stdout()
    result = run_diagnosis(args.database, user_id=args.user_id)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_diagnosis_text_report(result))
    return 1 if result.get("error") else 0
