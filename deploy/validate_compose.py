from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


EXPECTED_SERVICES = {
    "stack-init",
    "stack-maintenance",
    "model-gateway",
    "memory-gateway",
}


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _mounts(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = service.get("volumes") or []
    result = {item.get("target"): item for item in values}
    _require(len(result) == len(values), "duplicate_mount")
    return result


def validate_compose(
    configuration: dict[str, Any],
    *,
    init_image: str,
    model_image: str,
    memory_image: str,
    host: str,
    port: str,
    credential_directory: str,
    publish_ingress: bool = True,
) -> None:
    services = configuration.get("services") or {}
    _require(set(services) == EXPECTED_SERVICES, "service_set")
    images = {
        "stack-init": init_image,
        "stack-maintenance": init_image,
        "model-gateway": model_image,
        "memory-gateway": memory_image,
    }
    expected_service_fields = {
        "memory-gateway": {
            "cap_drop",
            "command",
            "depends_on",
            "entrypoint",
            "environment",
            "healthcheck",
            "image",
            "init",
            "logging",
            "networks",
            "pull_policy",
            "read_only",
            "restart",
            "security_opt",
            "tmpfs",
            "user",
            "volumes",
        }
        | ({"ports"} if publish_ingress else set()),
        "model-gateway": {
            "cap_drop",
            "command",
            "depends_on",
            "entrypoint",
            "environment",
            "healthcheck",
            "image",
            "init",
            "logging",
            "networks",
            "pull_policy",
            "read_only",
            "restart",
            "security_opt",
            "tmpfs",
            "user",
            "volumes",
        },
        "stack-init": {
            "cap_add",
            "cap_drop",
            "command",
            "entrypoint",
            "environment",
            "image",
            "network_mode",
            "pull_policy",
            "read_only",
            "restart",
            "security_opt",
            "tmpfs",
            "volumes",
        },
        "stack-maintenance": {
            "cap_add",
            "cap_drop",
            "command",
            "entrypoint",
            "environment",
            "image",
            "network_mode",
            "profiles",
            "pull_policy",
            "read_only",
            "restart",
            "security_opt",
            "tmpfs",
            "user",
            "volumes",
        },
    }
    for name, expected in images.items():
        service = services[name]
        _require(set(service) == expected_service_fields[name], f"{name}_fields")
        _require(service.get("image") == expected, f"{name}_image")
        _require(service.get("pull_policy") == "missing", f"{name}_pull_policy")
        _require(service.get("read_only") is True, f"{name}_read_only")
        _require(set(service.get("cap_drop") or []) == {"ALL"}, f"{name}_cap_drop")
        _require(
            service.get("security_opt") == ["no-new-privileges:true"],
            f"{name}_no_new_privileges",
        )
        _require(not service.get("privileged", False), f"{name}_privileged")
        _require(not service.get("devices"), f"{name}_devices")
        for forbidden_field in (
            "volumes_from",
            "secrets",
            "configs",
            "pid",
            "ipc",
            "userns_mode",
            "uts",
            "runtime",
            "post_start",
            "pre_stop",
            "group_add",
            "gpus",
            "device_cgroup_rules",
            "sysctls",
            "credential_spec",
            "cgroup",
            "cgroup_parent",
        ):
            _require(forbidden_field not in service, f"{name}_{forbidden_field}")

    _require(
        memory_image
        and services["memory-gateway"].get("tmpfs")
        == ["/tmp:size=128m,mode=1777"],
        "memory_tmpfs",
    )
    _require(
        services["model-gateway"].get("tmpfs")
        == ["/tmp:size=64m,mode=1777"],
        "model_tmpfs",
    )
    _require(
        services["stack-init"].get("tmpfs") == ["/tmp:size=64m,mode=1777"],
        "init_tmpfs",
    )
    _require(
        services["stack-maintenance"].get("tmpfs")
        == ["/tmp:size=128m,mode=1777"],
        "maintenance_tmpfs",
    )

    _require(not services["memory-gateway"].get("cap_add"), "memory_cap_add")
    _require(not services["model-gateway"].get("cap_add"), "model_cap_add")
    expected_maintenance_caps = {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
    _require(
        set(services["stack-init"].get("cap_add") or [])
        == expected_maintenance_caps,
        "init_cap_add",
    )
    _require(
        set(services["stack-maintenance"].get("cap_add") or [])
        == expected_maintenance_caps,
        "maintenance_cap_add",
    )

    memory = services["memory-gateway"]
    model = services["model-gateway"]
    initializer = services["stack-init"]
    maintenance = services["stack-maintenance"]
    expected_logging = {
        "driver": "json-file",
        "options": {"max-file": "3", "max-size": "10m"},
    }
    _require(memory.get("logging") == expected_logging, "memory_logging")
    _require(model.get("logging") == expected_logging, "model_logging")
    _require(str(memory.get("user")) == "10001:10001", "memory_uid")
    _require(str(model.get("user")) == "10002:10002", "model_uid")
    _require(str(maintenance.get("user")) == "0:0", "maintenance_uid")
    init_user = initializer.get("user")
    _require(
        init_user in {None, ""}
        or bool(re.fullmatch(r"[0-9]+:[0-9]+", str(init_user))),
        "init_uid",
    )
    _require(memory.get("init") is True and model.get("init") is True, "runtime_init")
    _require(memory.get("restart") == "unless-stopped", "memory_restart")
    _require(model.get("restart") == "unless-stopped", "model_restart")
    _require(initializer.get("restart") == "no", "init_restart")
    _require(maintenance.get("restart") == "no", "maintenance_restart")
    _require(
        not memory.get("command")
        and not memory.get("entrypoint")
        and not model.get("command")
        and not model.get("entrypoint")
        and not initializer.get("command")
        and not initializer.get("entrypoint"),
        "runtime_process_contract",
    )
    _require(
        maintenance.get("entrypoint") == ["memgw"]
        and not maintenance.get("command"),
        "maintenance_process_contract",
    )
    _require(
        maintenance.get("profiles") == ["maintenance"]
        and not memory.get("profiles")
        and not model.get("profiles")
        and not initializer.get("profiles"),
        "profiles",
    )
    model_dependencies = model.get("depends_on") or {}
    memory_dependencies = memory.get("depends_on") or {}
    _require(set(model_dependencies) == {"stack-init"}, "model_dependencies")
    _require(
        model_dependencies["stack-init"].get("condition")
        == "service_completed_successfully"
        and model_dependencies["stack-init"].get("required") is True,
        "model_init_dependency",
    )
    _require(
        set(memory_dependencies) == {"stack-init", "model-gateway"},
        "memory_dependencies",
    )
    _require(
        memory_dependencies["stack-init"].get("condition")
        == "service_completed_successfully"
        and memory_dependencies["stack-init"].get("required") is True
        and memory_dependencies["model-gateway"].get("condition")
        == "service_healthy"
        and memory_dependencies["model-gateway"].get("required") is True,
        "memory_dependency_contract",
    )
    expected_healthchecks = {
        "memory-gateway": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2026/health',timeout=3).status==200 else 1)",
            ],
            "timeout": "5s",
            "interval": "10s",
            "retries": 12,
            "start_period": "1m0s",
        },
        "model-gateway": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2030/health',timeout=3).status==200 else 1)",
            ],
            "timeout": "5s",
            "interval": "10s",
            "retries": 12,
            "start_period": "20s",
        },
    }
    _require(
        memory.get("healthcheck") == expected_healthchecks["memory-gateway"],
        "memory_healthcheck",
    )
    _require(
        model.get("healthcheck") == expected_healthchecks["model-gateway"],
        "model_healthcheck",
    )
    _require(
        set((memory.get("networks") or {}).keys()) == {"backend", "ingress"},
        "memory_networks",
    )
    _require(
        all(value is None for value in (memory.get("networks") or {}).values()),
        "memory_network_options",
    )
    _require(
        set((model.get("networks") or {}).keys())
        == {"provider-egress", "backend"},
        "model_networks",
    )
    _require(
        all(value is None for value in (model.get("networks") or {}).values()),
        "model_network_options",
    )
    _require(
        initializer.get("network_mode") == "none"
        and not initializer.get("networks"),
        "init_network",
    )
    _require(
        maintenance.get("network_mode") == "none"
        and not maintenance.get("networks"),
        "maintenance_network",
    )
    _require(
        not model.get("ports")
        and not initializer.get("ports")
        and not maintenance.get("ports"),
        "private_ports",
    )
    ports = memory.get("ports") or []
    if publish_ingress:
        _require(len(ports) == 1, "ingress_port_count")
        published = ports[0]
        _require(
            published
            == {
                "mode": "ingress",
                "host_ip": host,
                "target": 2026,
                "published": str(port),
                "protocol": "tcp",
            },
            "ingress_bind",
        )
    else:
        _require(not ports, "internal_candidate_ports")

    memory_mounts = _mounts(memory)
    model_mounts = _mounts(model)
    init_mounts = _mounts(initializer)
    maintenance_mounts = _mounts(maintenance)

    def volume_mount(source: str, target: str, *, read_only: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": "volume",
            "source": source,
            "target": target,
            "volume": {},
        }
        if read_only:
            value["read_only"] = True
        return value

    _require(set(memory_mounts) == {"/data", "/secrets"}, "memory_mounts")
    _require(
        memory_mounts["/data"] == volume_mount("memory-data", "/data"),
        "memory_data",
    )
    _require(
        memory_mounts["/secrets"]
        == volume_mount("memory-secrets", "/secrets", read_only=True),
        "memory_secrets",
    )
    _require(set(model_mounts) == {"/data", "/secrets"}, "model_mounts")
    _require(
        model_mounts["/data"] == volume_mount("model-data", "/data"),
        "model_data",
    )
    _require(
        model_mounts["/secrets"] == volume_mount("model-secrets", "/secrets"),
        "model_secrets",
    )
    _require(
        set(init_mounts)
        == {
            "/memory-data",
            "/memory-secrets",
            "/model-data",
            "/model-secrets",
            "/credentials",
        },
        "init_mounts",
    )
    for target, source in (
        ("/memory-data", "memory-data"),
        ("/memory-secrets", "memory-secrets"),
        ("/model-data", "model-data"),
        ("/model-secrets", "model-secrets"),
    ):
        _require(
            init_mounts[target] == volume_mount(source, target),
            "init_" + target,
        )
    credential_mount = init_mounts["/credentials"]
    _require(
        credential_mount
        == {
            "type": "bind",
            "source": os.path.normpath(credential_directory),
            "target": "/credentials",
        },
        "credential_source",
    )
    _require(
        set(maintenance_mounts) == {"/data", "/secrets", "/model-data"},
        "maintenance_mounts",
    )
    for target, source in (
        ("/data", "memory-data"),
        ("/secrets", "memory-secrets"),
        ("/model-data", "model-data"),
    ):
        _require(
            maintenance_mounts[target] == volume_mount(source, target),
            "maintenance_" + target,
        )

    project = str(configuration.get("name") or "")
    _require(bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)), "project_name")
    networks = configuration.get("networks") or {}
    _require(
        set(networks) == {"backend", "ingress", "provider-egress"},
        "network_set",
    )
    _require(
        networks["backend"]
        == {"name": f"{project}_backend", "ipam": {}, "internal": True},
        "backend_network",
    )
    _require(
        networks["ingress"] == {"name": f"{project}_ingress", "ipam": {}},
        "ingress_network",
    )
    _require(
        networks["provider-egress"]
        == {"name": f"{project}_provider-egress", "ipam": {}},
        "provider_egress_network",
    )
    volumes = configuration.get("volumes") or {}
    _require(
        set(volumes)
        == {"memory-data", "memory-secrets", "model-data", "model-secrets"},
        "volume_set",
    )
    for name in volumes:
        _require(volumes[name] == {"name": f"{project}_{name}"}, f"{name}_volume")

    expected_environment = {
        "memory-gateway": {
            "MEMGW_HOME": "/data/config",
            "MEMGW_PROJECT_ROOT": "/app/services/memory-gateway",
            "MEMGW_SETTINGS_PATH": "/secrets/settings.env",
        },
        "model-gateway": {
            "MODEL_GATEWAY_HOME": "/data",
            "MODEL_GATEWAY_SECRETS_PATH": "/secrets/secrets.env",
        },
        "stack-init": {
            "MEMGW_HOME": "/memory-data/config",
            "MEMGW_SETTINGS_PATH": "/memory-secrets/settings.env",
            "MODEL_GATEWAY_HOME": "/model-data",
            "MODEL_GATEWAY_SECRETS_PATH": "/model-secrets/secrets.env",
        },
        "stack-maintenance": {
            "MEMGW_HOME": "/data/config",
            "MEMGW_PROJECT_ROOT": "/app/services/memory-gateway",
            "MEMGW_SETTINGS_PATH": "/secrets/settings.env",
            "MODEL_GATEWAY_HOME": "/model-data",
        },
    }
    for name, values in expected_environment.items():
        actual = services[name].get("environment") or {}
        if name == "stack-init":
            _require(set(actual) == set(values) | {"HOST_UID", "HOST_GID"}, "stack-init_environment")
            _require(
                all(
                    value in {None, ""}
                    or bool(re.fullmatch(r"[0-9]+", str(value)))
                    for value in (actual.get("HOST_UID"), actual.get("HOST_GID"))
                ),
                "stack-init_host_ids",
            )
        else:
            _require(set(actual) == set(values), f"{name}_environment")
        _require(
            all(actual.get(key) == value for key, value in values.items()),
            f"{name}_environment_values",
        )
    _require(
        not re.search(
            r"(?:GATEWAY_API_KEY|MEMORY_CONSOLE_ADMIN_KEY)",
            json.dumps(configuration),
        ),
        "access_secret_env",
    )


def main() -> int:
    if len(sys.argv) not in {7, 8, 9}:
        print("invalid validator arguments", file=sys.stderr)
        return 2
    try:
        internal = sys.argv[-1] == "internal"
        input_path: str | None = None
        if len(sys.argv) == 8 and not internal:
            input_path = sys.argv[7]
        elif len(sys.argv) == 9 and internal:
            input_path = sys.argv[7]
        elif len(sys.argv) == 9:
            raise ValueError("validator_arguments")
        if input_path is not None:
            with open(input_path, encoding="utf-8") as stream:
                configuration = json.load(stream)
        else:
            configuration = json.load(sys.stdin)
        validate_compose(
            configuration,
            init_image=sys.argv[1],
            model_image=sys.argv[2],
            memory_image=sys.argv[3],
            host=sys.argv[4],
            port=sys.argv[5],
            credential_directory=sys.argv[6],
            publish_ingress=not internal,
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"unsafe compose topology: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
