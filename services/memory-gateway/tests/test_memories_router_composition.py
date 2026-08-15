from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.api import memories
from app.api.memories import (
    common,
    conversation,
    core,
    crud,
    evaluation,
    export,
    graph,
    import_conversations,
    item,
    purge,
    review,
    search,
)
from app.main import app


DOMAIN_MODULES = (
    conversation,
    core,
    crud,
    evaluation,
    export,
    graph,
    import_conversations,
    purge,
    review,
    search,
    item,
)


def test_memories_router_composes_all_domain_owned_routes() -> None:
    child_routers = [module.router for module in DOMAIN_MODULES]
    assert len({id(router) for router in child_routers}) == len(DOMAIN_MODULES)

    expected_operations: set[tuple[str, str]] = set()
    for module, router in zip(DOMAIN_MODULES, child_routers, strict=True):
        for route in router.routes:
            assert isinstance(route, APIRoute)
            assert route.endpoint.__module__ == module.__name__
            for method in route.methods:
                expected_operations.add((method, f"/memories{route.path}"))

    assert len(expected_operations) == 64
    schema = app.openapi()
    actual_operations = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        if path.startswith("/memories")
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert actual_operations == expected_operations
    assert all(memories.router is not router for router in child_routers)


def test_memories_domains_do_not_use_star_import_registration() -> None:
    assert not hasattr(common, "router")
    for module in DOMAIN_MODULES:
        source_path = Path(module.__file__ or "")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.api.memories.common"
            and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        )
