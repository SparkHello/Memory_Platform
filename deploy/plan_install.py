from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LAYOUTS = {"fresh", "split"}
_READINESS = {"ready", "not_ready", "absent", "unknown"}


@dataclass(frozen=True)
class InstallFacts:
    layout: str
    candidate_images: tuple[str, str, str]
    current_images: tuple[str | None, str | None, str | None]
    candidate_managed_config: str
    current_managed_config: str | None
    memory_readiness: str
    model_readiness: str


@dataclass(frozen=True)
class InstallPlan:
    action: str
    reason: str
    repair_scope: str
    accept_memory_readiness: bool
    accept_model_readiness: bool
    accept_host_readiness: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "action": self.action,
            "reason": self.reason,
            "repair_scope": self.repair_scope,
            "acceptance": {
                "memory_readiness": self.accept_memory_readiness,
                "model_readiness": self.accept_model_readiness,
                "host_readiness": self.accept_host_readiness,
            },
        }

    def as_tsv(self) -> str:
        fields = (
            "1",
            self.action,
            self.reason,
            self.repair_scope,
            "1" if self.accept_memory_readiness else "0",
            "1" if self.accept_model_readiness else "0",
            "1" if self.accept_host_readiness else "0",
        )
        return "\t".join(fields)


def _valid_digest(value: str | None) -> bool:
    return value is not None and _DIGEST.fullmatch(value) is not None


def plan_install(facts: InstallFacts) -> InstallPlan:
    if facts.layout not in _LAYOUTS:
        raise ValueError("layout")
    if not all(_valid_digest(item) for item in facts.candidate_images):
        raise ValueError("candidate_images")
    if not _valid_digest(facts.candidate_managed_config):
        raise ValueError("candidate_managed_config")
    if facts.memory_readiness not in _READINESS:
        raise ValueError("memory_readiness")
    if facts.model_readiness not in _READINESS:
        raise ValueError("model_readiness")
    if "unknown" in {facts.memory_readiness, facts.model_readiness}:
        raise ValueError("unknown_readiness")

    accept_memory = facts.memory_readiness == "ready"
    accept_model = facts.model_readiness == "ready"
    acceptance = {
        "accept_memory_readiness": accept_memory,
        "accept_model_readiness": accept_model,
        "accept_host_readiness": accept_memory,
    }
    if facts.layout == "fresh":
        if any(item is not None for item in facts.current_images):
            raise ValueError("fresh_current_images")
        if facts.current_managed_config is not None:
            raise ValueError("fresh_current_managed_config")
        if {facts.memory_readiness, facts.model_readiness} != {"absent"}:
            raise ValueError("fresh_readiness")
        return InstallPlan(
            action="upgrade",
            reason="fresh_install",
            repair_scope="none",
            **acceptance,
        )

    if not all(item is None or _valid_digest(item) for item in facts.current_images):
        raise ValueError("current_images")
    if not _valid_digest(facts.current_managed_config):
        raise ValueError("current_managed_config")
    images_changed = facts.current_images != facts.candidate_images
    config_changed = facts.current_managed_config != facts.candidate_managed_config
    if images_changed or config_changed:
        if images_changed and config_changed:
            reason = "image_and_config_change"
        elif images_changed:
            reason = "image_change"
        else:
            reason = "managed_config_change"
        return InstallPlan(
            action="upgrade",
            reason=reason,
            repair_scope="none",
            **acceptance,
        )

    degraded = {
        name
        for name, readiness in (
            ("memory", facts.memory_readiness),
            ("model", facts.model_readiness),
        )
        if readiness != "ready"
    }
    if not degraded:
        return InstallPlan(
            action="noop",
            reason="already_current",
            repair_scope="none",
            **acceptance,
        )
    if degraded == {"memory", "model"}:
        repair_scope = "both"
    else:
        repair_scope = degraded.pop()
    return InstallPlan(
        action="repair",
        reason="service_repair",
        repair_scope=repair_scope,
        **acceptance,
    )


def _optional_digest(value: str) -> str | None:
    return None if value == "-" else value


def main() -> int:
    if len(sys.argv) not in {12, 13}:
        print("invalid planner arguments", file=sys.stderr)
        return 2
    output_format = "json" if len(sys.argv) == 12 else sys.argv[12]
    if output_format not in {"json", "tsv"}:
        print("invalid planner output format", file=sys.stderr)
        return 2
    try:
        plan = plan_install(
            InstallFacts(
                layout=sys.argv[1],
                candidate_images=(sys.argv[2], sys.argv[3], sys.argv[4]),
                current_images=(
                    _optional_digest(sys.argv[5]),
                    _optional_digest(sys.argv[6]),
                    _optional_digest(sys.argv[7]),
                ),
                candidate_managed_config=sys.argv[8],
                current_managed_config=_optional_digest(sys.argv[9]),
                memory_readiness=sys.argv[10],
                model_readiness=sys.argv[11],
            )
        )
    except (TypeError, ValueError) as exc:
        print(f"invalid install facts: {exc}", file=sys.stderr)
        return 1
    if output_format == "tsv":
        print(plan.as_tsv())
    else:
        print(json.dumps(plan.as_dict(), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
