"""Space workbench CRUD: create, rename, color, archive, delete empty."""

from __future__ import annotations


def test_memory_space_crud_lifecycle(client, auth_headers) -> None:
    created = client.post(
        "/memories/spaces",
        headers=auth_headers,
        json={
            "name": "工作项目",
            "color": "#4f46e5",
            "description": "长期项目相关",
            "sort_order": 10,
        },
    )
    assert created.status_code == 201, created.text
    space = created.json()["space"]
    space_id = space["id"]
    assert space["name"] == "工作项目"
    assert space["color"] == "#4F46E5"
    assert space["description"] == "长期项目相关"
    assert space["sort_order"] == 10
    assert space["archived"] == 0

    listed = client.get("/memories/spaces", headers=auth_headers)
    assert listed.status_code == 200
    assert any(item["id"] == space_id for item in listed.json()["data"])

    patched = client.patch(
        f"/memories/spaces/{space_id}",
        headers=auth_headers,
        json={"name": "重点项目", "color": None, "sort_order": 1},
    )
    assert patched.status_code == 200, patched.text
    updated = patched.json()["space"]
    assert updated["name"] == "重点项目"
    assert updated["color"] is None
    assert updated["sort_order"] == 1

    archived = client.post(
        f"/memories/spaces/{space_id}/archive",
        headers=auth_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["space"]["archived"] == 1

    active_only = client.get("/memories/spaces", headers=auth_headers)
    assert all(item["id"] != space_id for item in active_only.json()["data"])

    with_archived = client.get(
        "/memories/spaces",
        headers=auth_headers,
        params={"include_archived": True},
    )
    assert any(item["id"] == space_id for item in with_archived.json()["data"])

    restored = client.post(
        f"/memories/spaces/{space_id}/unarchive",
        headers=auth_headers,
    )
    assert restored.status_code == 200
    assert restored.json()["space"]["archived"] == 0

    deleted = client.delete(f"/memories/spaces/{space_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = client.get(f"/memories/spaces/{space_id}", headers=auth_headers)
    assert missing.status_code == 404


def test_memory_space_delete_rejects_non_empty(client, auth_headers, memory_store) -> None:
    created = client.post(
        "/memories/spaces",
        headers=auth_headers,
        json={"name": "绑定测试"},
    )
    space_id = created.json()["space"]["id"]

    memory_store.create_memory(
        user_id="default",
        content="我在用空间绑定测试",
        space_ids=[space_id],
    )

    response = client.delete(f"/memories/spaces/{space_id}", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "space_not_empty"


def test_memory_space_rejects_invalid_color(client, auth_headers) -> None:
    response = client.post(
        "/memories/spaces",
        headers=auth_headers,
        json={"name": "坏颜色", "color": "blue"},
    )
    assert response.status_code == 422
