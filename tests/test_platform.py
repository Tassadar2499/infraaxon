import json
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from infraaxon.api import create_app
from infraaxon.store import Store
from infraaxon.llm import parse_assessment


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_KEY", "test-operator-key")
    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer test-operator-key"
        client.store = store
        yield client
    store.close()


def env(client, name="Test"):
    return client.post("/api/environments", json={"name": name}).json()["id"]


def component(client, eid, **extra):
    return client.post(
        f"/api/environments/{eid}/components",
        json={"name": "Cache", "type": "redis", "endpoint": "redis://localhost:6379", **extra},
    )


def test_auth_required(client):
    assert client.get("/api/environments", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_login_cookie_is_http_only(client):
    response = client.post("/api/login", json={"token": "test-operator-key"})
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_secrets_are_encrypted_and_not_returned(client):
    id = env(client)
    r = component(client, id, secrets={"password": "very-private"})
    assert r.status_code == 201
    assert "very-private" not in r.text
    assert "very-private" not in client.get(f"/api/environments/{id}/components").text
    stored = client.store.get("component", r.json()["id"])
    assert "very-private" not in stored["encrypted_secrets"]
    assert client.store.decrypt(stored["encrypted_secrets"])["password"] == "very-private"


def test_cross_environment_dependency_rejected(client):
    a, b = env(client, "A"), env(client, "B")
    ca = component(client, a).json()["id"]
    assert component(client, b, dependencies=[ca]).status_code == 422


def test_component_web_url_lifecycle_and_legacy_updates(client):
    eid = env(client)
    url = "https://grafana.example.test:8443/d/main?orgId=1#panel"
    created = component(client, eid, web_url=url)
    assert created.status_code == 201
    c = created.json()
    assert c["web_url"] == url
    body = {k: c[k] for k in ("name", "type", "endpoint", "description", "settings", "enabled", "dependencies")}
    path = f"/api/components/{c['id']}"
    assert client.put(path, json=body).json()["web_url"] == url
    replacement = "http://localhost:13000"
    assert client.put(path, json={**body, "web_url": replacement}).json()["web_url"] == replacement
    assert client.get(f"/api/environments/{eid}/components").json()[0]["web_url"] == replacement
    assert client.put(path, json={**body, "web_url": ""}).json()["web_url"] == ""
    assert client.put(path, json=body).json()["web_url"] == ""

    # Simulate a record written before web_url existed.
    stored = client.store.get("component", c["id"])
    stored.pop("web_url")
    client.store.put("component", stored)
    assert client.get(f"/api/environments/{eid}/components").status_code == 200
    assert client.put(path, json=body).json()["web_url"] == ""
    assert component(client, eid).json()["web_url"] == ""


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "ftp://example.test", "/relative", "//example.test",
    "http://", "http://user:password@example.test", "https://user@example.test", "http://example.test:99999",
    "http://example.test\n/path", "http://exa mple.test", "http://example.test\\@evil.test",
])
def test_component_rejects_invalid_web_urls(client, url):
    eid = env(client)
    assert component(client, eid, web_url=url).status_code == 422


def test_self_dependency_rejected(client):
    id = env(client)
    c = component(client, id).json()
    r = client.put(
        f"/api/components/{c['id']}",
        json={"name": "X", "type": "redis", "endpoint": "redis://localhost:6379", "dependencies": [c["id"]]},
    )
    assert r.status_code == 422


def test_agent_token_cannot_read_another_component(client):
    eid = env(client)
    a = component(client, eid, enabled=True).json()
    b = component(client, eid, enabled=True).json()
    token = client.store.decrypt(client.store.get("component", a["id"])["encrypted_secrets"])["_agent_token"]
    assert client.get(f"/internal/components/{a['id']}/config", headers={"X-Agent-Token": token}).status_code == 200
    assert client.get(f"/internal/components/{b['id']}/config", headers={"X-Agent-Token": token}).status_code == 401


def test_disabled_agent_cannot_fetch_credentials(client):
    eid = env(client)
    c = component(client, eid).json()
    token = client.store.decrypt(client.store.get("component", c["id"])["encrypted_secrets"])["_agent_token"]
    assert client.get(f"/internal/components/{c['id']}/config", headers={"X-Agent-Token": token}).status_code == 401


def test_invalid_targets_and_unknown_adapters(client):
    id = env(client)
    assert component(client, id, type="shell").status_code == 422
    assert component(client, id, endpoint="file:///etc/passwd").status_code == 422
    assert component(client, id, endpoint="redis://user:secret@localhost").status_code == 422
    assert component(client, id, secrets={"_agent_token": "override"}).status_code == 422


def test_deletion_cleans_edges_but_retains_history(client):
    eid = env(client)
    a = component(client, eid).json()
    b = component(client, eid, dependencies=[a["id"]]).json()
    client.store.put("investigation", {"id": "past", "environment_id": eid, "status": "completed", "results": []})
    assert client.delete(f"/api/components/{a['id']}").status_code == 200
    assert client.store.get("component", b["id"])["dependencies"] == []
    assert client.get("/api/investigations/past").status_code == 200


def test_unknown_evidence_rejected():
    result = {"summary": "Failure", "hypotheses": [{"cause": "Redis down", "evidence_ids": ["invented"]}]}
    with pytest.raises(ValueError):
        parse_assessment({"content": json.dumps(result)}, [{"id": "real"}])


def test_supported_assessment():
    result = {"summary": "Check failed", "hypotheses": [{"cause": "Connection failed", "evidence_ids": ["real"]}]}
    assert parse_assessment({"content": json.dumps(result)}, [{"id": "real"}])["summary"] == "Check failed"


def test_updates_preserve_registry_order(client):
    eid = env(client)
    a = component(client, eid).json()
    b = component(client, eid).json()
    stored = client.store.get("component", a["id"])
    stored["agent_state"] = "running"
    client.store.put("component", stored)
    assert [c["id"] for c in client.store.all("component")] == [a["id"], b["id"]]


def test_restart_marks_running_jobs_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_KEY", "test")
    store = Store(str(tmp_path / "restart.db"), Fernet.generate_key().decode())
    store.put("investigation", {"id": "running", "status": "running", "results": [{"evidence": [{"id": "saved"}]}]})
    with TestClient(create_app(store)):
        record = store.get("investigation", "running")
        assert record["status"] == "interrupted"
        assert record["results"][0]["evidence"][0]["id"] == "saved"
    store.close()


@pytest.mark.asyncio
async def test_unreachable_adapter_produces_real_error():
    from infraaxon.adapters import observe

    result = await observe(
        {"id": "test", "type": "http", "endpoint": "http://127.0.0.1:1", "settings": {}}, {"token": "never-expose-this"}
    )
    assert result["ok"] is False
    assert "never-expose-this" not in result["data"]


def test_provisioner_only_reconciles_owned_containers(monkeypatch):
    from unittest.mock import MagicMock
    from infraaxon.provisioner import reconcile

    monkeypatch.setenv("LLM_KEY", "local")
    docker = MagicMock()
    docker.containers.list.return_value = []
    reconcile(docker, [{"id": "component", "revision": "1", "token": "private"}])
    assert docker.containers.list.call_args.kwargs["filters"] == {"label": "io.infraaxon.owner=infraaxon-local"}
    kwargs = docker.containers.run.call_args.kwargs
    assert "volumes" not in kwargs
    assert kwargs["read_only"] is True


def test_provisioner_replaces_old_image_without_resolving_deleted_image(monkeypatch):
    from unittest.mock import MagicMock, PropertyMock
    from infraaxon.provisioner import reconcile

    monkeypatch.setenv("LLM_KEY", "local")
    docker = MagicMock()
    docker.images.get.return_value.id = "sha256:new"
    old = MagicMock()
    old.labels = {"io.infraaxon.component": "c", "io.infraaxon.revision": "1"}
    old.attrs = {"Image": "sha256:old"}
    type(old).image = PropertyMock(side_effect=RuntimeError("Old image was deleted"))
    docker.containers.list.return_value = [old]
    reconcile(docker, [{"id": "c", "revision": "1", "token": "private"}])
    old.remove.assert_called_once_with(force=True)
    docker.containers.run.assert_called_once()


@pytest.mark.asyncio
async def test_model_final_json_wrapped_as_tool_is_validated_not_executed(monkeypatch):
    from infraaxon import agent
    from unittest.mock import AsyncMock

    monkeypatch.setattr(agent, "TOKEN", "scoped-token")
    monkeypatch.setattr(
        agent,
        "spec",
        AsyncMock(
            return_value={
                "component": {"name": "HTTP", "type": "http", "description": "", "settings": {}},
                "secrets": {},
            }
        ),
    )
    observation = AsyncMock(return_value={"id": "observed", "ok": True})
    monkeypatch.setattr(agent, "observe", observation)
    result = {"summary": "Available", "hypotheses": [{"cause": "HTTP returned 200", "evidence_ids": ["observed"]}]}
    monkeypatch.setattr(
        agent,
        "completion",
        AsyncMock(
            return_value={
                "tool_calls": [
                    {"id": "tool1", "function": {"name": "inspect_component", "arguments": json.dumps(result)}}
                ]
            }
        ),
    )
    answer = await agent.diagnose(agent.DiagnosisInput(question="Health?"), "Bearer scoped-token")
    assert answer["assessment"]["summary"] == "Available"
    observation.assert_awaited_once()
