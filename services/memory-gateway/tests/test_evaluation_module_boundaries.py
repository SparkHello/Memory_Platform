from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from app.memory import evaluation, evaluation_cli, evaluation_workspace


SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _load_script(filename: str, module_name: str):
    path = SERVICE_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluation_core_has_no_cli_parser_dependency() -> None:
    source_path = Path(evaluation.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "argparse" not in imported_modules
    assert evaluation_cli.recall_cli_main.__module__ == evaluation_cli.__name__
    assert evaluation_cli.diagnosis_cli_main.__module__ == evaluation_cli.__name__


def test_evaluation_core_has_no_direct_workspace_io() -> None:
    source_path = Path(evaluation.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_path_calls = {
        "glob",
        "mkdir",
        "open",
        "read_text",
        "replace",
        "unlink",
        "write_text",
    }
    direct_path_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_path_calls
    }
    direct_sqlite_connects = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
        and node.func.attr == "connect"
    ]

    assert direct_path_calls == set()
    assert direct_sqlite_connects == []
    assert evaluation_workspace.initialize_eval_workspace.__module__ == (
        evaluation_workspace.__name__
    )
    assert evaluation_workspace.snapshot_readonly.__module__ == (
        evaluation_workspace.__name__
    )
    assert evaluation_workspace.load_labels_file.__module__ == (
        evaluation_workspace.__name__
    )
    assert evaluation_workspace.save_eval_result_file.__module__ == (
        evaluation_workspace.__name__
    )


def test_legacy_scripts_delegate_to_the_cli_adapter() -> None:
    recall_script = _load_script("eval_recall.py", "eval_recall_boundary_test")
    diagnosis_script = _load_script(
        "diagnose_memory_health.py",
        "diagnose_memory_health_boundary_test",
    )
    assert recall_script.recall_cli_main is evaluation_cli.recall_cli_main
    assert diagnosis_script.diagnosis_cli_main is evaluation_cli.diagnosis_cli_main


def test_workspace_compatibility_api_stays_owned_by_workspace_module() -> None:
    assert (
        evaluation.delete_user_eval_workspace
        is evaluation_workspace.delete_user_eval_workspace
    )
    assert evaluation_workspace.stage_user_eval_workspace.__module__ == (
        evaluation_workspace.__name__
    )
    assert evaluation_workspace.restore_staged_eval_workspace.__module__ == (
        evaluation_workspace.__name__
    )
