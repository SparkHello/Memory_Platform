from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
PLANNER = ROOT / "deploy" / "plan_install.py"
DIGESTS = tuple(f"sha256:{character * 64}" for character in "abc")
CONFIG = "sha256:" + "d" * 64


def _load_planner():
    spec = importlib.util.spec_from_file_location("memory_platform_install_planner", PLANNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _split_facts(**changes):
    planner = _load_planner()
    facts = planner.InstallFacts(
        layout="split",
        candidate_images=DIGESTS,
        current_images=DIGESTS,
        candidate_managed_config=CONFIG,
        current_managed_config=CONFIG,
        memory_readiness="ready",
        model_readiness="ready",
    )
    return planner, replace(facts, **changes)


def test_fresh_install_is_an_upgrade_without_readiness_regression_gates() -> None:
    planner = _load_planner()
    plan = planner.plan_install(
        planner.InstallFacts(
            layout="fresh",
            candidate_images=DIGESTS,
            current_images=(None, None, None),
            candidate_managed_config=CONFIG,
            current_managed_config=None,
            memory_readiness="absent",
            model_readiness="absent",
        )
    )

    assert plan.action == "upgrade"
    assert plan.reason == "fresh_install"
    assert plan.repair_scope == "none"
    assert not plan.accept_memory_readiness
    assert not plan.accept_model_readiness
    assert not plan.accept_host_readiness


def test_fresh_install_rejects_even_one_current_image() -> None:
    planner = _load_planner()

    with pytest.raises(ValueError, match="fresh_current_images"):
        planner.plan_install(
            planner.InstallFacts(
                layout="fresh",
                candidate_images=DIGESTS,
                current_images=(DIGESTS[0], None, None),
                candidate_managed_config=CONFIG,
                current_managed_config=None,
                memory_readiness="absent",
                model_readiness="absent",
            )
        )


def test_exact_ready_split_stack_is_noop() -> None:
    planner, facts = _split_facts()

    plan = planner.plan_install(facts)

    assert plan.action == "noop"
    assert plan.reason == "already_current"
    assert plan.repair_scope == "none"
    assert plan.accept_memory_readiness
    assert plan.accept_model_readiness
    assert plan.accept_host_readiness


@pytest.mark.parametrize(
    ("memory", "model", "scope", "memory_gate", "model_gate"),
    (
        ("not_ready", "ready", "memory", False, True),
        ("absent", "ready", "memory", False, True),
        ("ready", "not_ready", "model", True, False),
        ("ready", "absent", "model", True, False),
        ("not_ready", "not_ready", "both", False, False),
        ("not_ready", "absent", "both", False, False),
        ("absent", "not_ready", "both", False, False),
        ("absent", "absent", "both", False, False),
    ),
)
def test_exact_but_degraded_split_stack_gets_targeted_repair(
    memory: str,
    model: str,
    scope: str,
    memory_gate: bool,
    model_gate: bool,
) -> None:
    planner, facts = _split_facts(
        memory_readiness=memory,
        model_readiness=model,
    )

    plan = planner.plan_install(facts)

    assert plan.action == "repair"
    assert plan.reason == "service_repair"
    assert plan.repair_scope == scope
    assert plan.accept_memory_readiness is memory_gate
    assert plan.accept_model_readiness is model_gate
    assert plan.accept_host_readiness is memory_gate


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"current_images": (DIGESTS[0], DIGESTS[1], None)}, "image_change"),
        (
            {"current_managed_config": "sha256:" + "e" * 64},
            "managed_config_change",
        ),
        (
            {
                "current_images": (None, DIGESTS[1], DIGESTS[2]),
                "current_managed_config": "sha256:" + "e" * 64,
            },
            "image_and_config_change",
        ),
    ),
)
def test_any_managed_drift_requires_upgrade(changes: dict, reason: str) -> None:
    planner, facts = _split_facts(**changes)

    plan = planner.plan_install(facts)

    assert plan.action == "upgrade"
    assert plan.reason == reason
    assert plan.repair_scope == "none"


@pytest.mark.parametrize(
    "changes",
    (
        {"memory_readiness": "unknown"},
        {"model_readiness": "unknown"},
        {"candidate_images": ("latest", DIGESTS[1], DIGESTS[2])},
        {"current_managed_config": "not-a-digest"},
        {"layout": "legacy"},
    ),
)
def test_invalid_or_unknown_facts_fail_closed(changes: dict) -> None:
    planner, facts = _split_facts(**changes)

    with pytest.raises(ValueError):
        planner.plan_install(facts)


def test_split_install_rejects_even_one_invalid_current_digest() -> None:
    planner, facts = _split_facts(
        current_images=(DIGESTS[0], "not-a-digest", DIGESTS[2]),
    )

    with pytest.raises(ValueError, match="current_images"):
        planner.plan_install(facts)


def test_cli_emits_stable_typed_json_and_tsv() -> None:
    arguments = [
        "split",
        *DIGESTS,
        *DIGESTS,
        CONFIG,
        CONFIG,
        "ready",
        "ready",
    ]
    json_result = subprocess.run(
        [sys.executable, str(PLANNER), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    tsv_result = subprocess.run(
        [sys.executable, str(PLANNER), *arguments, "tsv"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert json_result.returncode == 0, json_result.stderr
    assert json.loads(json_result.stdout) == {
        "acceptance": {
            "host_readiness": True,
            "memory_readiness": True,
            "model_readiness": True,
        },
        "action": "noop",
        "reason": "already_current",
        "repair_scope": "none",
        "version": 1,
    }
    assert tsv_result.returncode == 0, tsv_result.stderr
    assert tsv_result.stdout == "1\tnoop\talready_current\tnone\t1\t1\t1\n"
