import json
from unittest.mock import AsyncMock
import httpx
import pytest
from test_platform import client as client, component, env
from infraaxon.profiles import all_profiles, system_prompt
from infraaxon.models import ComponentInput, AgentDiagnosisInput
from infraaxon.adapters import observe, tool_schema, parse_metrics
from infraaxon.routing import rank_components, context_requests


def record(id, type="http", profile="", **kw):
    return {
        "id": id,
        "name": id,
        "type": type,
        "agent_profile": profile or type,
        "endpoint": "http://example.test",
        "enabled": True,
        "settings": {},
        "description": "",
        "dependencies": [],
        "context_sources": [],
        **kw,
    }


def test_all_declared_profiles_have_compatible_initial_tools():
    from pathlib import Path
    import importlib.util

    spec = importlib.util.spec_from_file_location("shop_agents", Path(__file__).parents[1] / "tools/shop_agents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    merged = module.merge_settings(
        {"queries": {"up": "up", "lag": "lag_metric"}, "health_path": "/health"},
        {"query": "operator_metric", "health_path": "/ready", "queries": {"custom": "custom_metric"}},
    )
    assert merged["queries"] == {"up": "operator_metric", "lag": "lag_metric", "custom": "custom_metric"}
    assert merged["health_path"] == "/ready"
    assert len(module.PROFILES) == 25
    for id in module.PROFILES.values():
        p = all_profiles()[id]
        c = record(id, p["type"], id)
        ComponentInput(name=id, type=p["type"], endpoint=c["endpoint"], agent_profile=id)
        assert all(check["action"] in p["actions"] for check in p["initial_checks"])
    prompts = [system_prompt(record(id, profile=id), "BASE") for id in ["shop-catalog", "shop-orders", "shop-worker"]]
    assert len(set(prompts)) == 3
    assert "TTL 30" in prompts[0] and "outbox" in prompts[1] and "lastProcessedAt" in prompts[2]


def test_profile_lifecycle_and_old_client_preservation(client):
    eid = env(client)
    source = component(client, eid, name="Logs", type="elasticsearch", endpoint="http://logs").json()
    data = dict(
        name="Orders",
        type="http",
        endpoint="http://orders",
        agent_profile="shop-orders",
        context_sources=[{"component_id": source["id"], "service_name": "shop-orders"}],
    )
    c = component(client, eid, **data).json()
    assert c["agent_profile"] == "shop-orders"
    old_body = {k: c[k] for k in ("name", "type", "endpoint", "description", "settings", "enabled", "dependencies")}
    changed = client.put("/api/components/" + c["id"], json=old_body).json()
    assert changed["agent_profile"] == "shop-orders" and changed["context_sources"] == c["context_sources"]
    assert client.get("/api/agent-profiles").status_code == 200
    assert client.delete("/api/components/" + source["id"]).status_code == 200
    stored = client.store.get("component", c["id"])
    assert stored["context_sources"] == []
    stored.pop("agent_profile")
    stored.pop("context_sources")
    client.store.put("component", stored)
    legacy = client.get(f"/api/environments/{eid}/components").json()[0]
    assert legacy["agent_profile"] == "http" and legacy["context_sources"] == []


def test_invalid_profiles_and_context_rejected(client):
    a, b = env(client, "A"), env(client, "B")
    logs = component(client, b, type="elasticsearch", endpoint="http://logs").json()
    assert component(client, a, agent_profile="shop-orders").status_code == 422
    assert component(client, a, agent_profile="invented").status_code == 422
    assert component(client, a, context_sources=[{"component_id": logs["id"]}]).status_code == 422
    redis = component(client, a).json()
    assert component(client, a, context_sources=[{"component_id": redis["id"]}]).status_code == 422
    for path in ("https://elsewhere.test", "//elsewhere.test/path", "/\\evil", "/path\n"):
        assert (
            component(client, a, type="http", endpoint="http://app", settings={"checks": {"escape": path}}).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_tools_reject_unsupported_actions_without_network(monkeypatch):
    from infraaxon import adapters

    read = AsyncMock()
    monkeypatch.setattr(adapters, "_read", read)
    for profile, kind, action in [
        ("redis", "redis", "search"),
        ("shop-worker", "http", "check"),
        ("toxiproxy", "http", "check"),
    ]:
        result = await observe(record("x", kind, profile), {}, action=action)
        assert not result["ok"]
        assert (
            action
            not in tool_schema(record("x", kind, profile))["function"]["parameters"]["properties"]["action"]["enum"]
        )
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "question,expected",
    [
        ("изображения пропали", "Images"),
        ("cache redis slow", "Redis"),
        ("заказы outbox", "Orders"),
        ("worker consumer lag", "Worker"),
    ],
)
def test_symptom_routing(question, expected):
    cs = [
        record("Images", "s3"),
        record("Redis", "redis"),
        record("Orders", profile="shop-orders"),
        record("Worker", profile="shop-worker"),
    ]
    ranked, _ = rank_components(cs, question)
    assert ranked[0][0]["name"] == expected


def test_target_priority_budgets_and_stability():
    cs = [record(str(i)) for i in range(12)]
    cs[8]["dependencies"] = ["5"]
    cs[8]["context_sources"] = [{"component_id": "4"}]
    cs[3]["dependencies"] = ["8"]
    ranked, skipped = rank_components(cs, "", "8", {"0": {"ok": False}})
    assert [c["id"] for c, _ in ranked[:4]] == ["8", "5", "4", "3"]
    assert len(ranked) == 6 and len(skipped) == 6
    assert rank_components(cs, "", direct=True)[0][0][0]["id"] == "0"
    assert rank_components(cs, "")[0] == rank_components(cs, "")[0]


def test_context_dedup_disabled_sources_and_limit():
    sources = [record(str(i), "elasticsearch") for i in range(10)]
    sources[-1]["enabled"] = False
    links = [{"component_id": s["id"]} for s in sources]
    targets = [record("a", context_sources=links), record("b", context_sources=links)]
    req, skip = context_requests(targets, sources, "failure", 15, None)
    assert len(req) == 8 and all(r["targets"] == ["a", "b"] for r in req)
    assert any("выключен" in x["reason"] for x in skip)
    assert any("восьми" in x["reason"] for x in skip)


@pytest.mark.asyncio
async def test_named_checks_and_metrics_cannot_take_model_urls(monkeypatch):
    from infraaxon import adapters

    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"result": []}, "status": "success"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        adapters.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    c = record("p", "prometheus")
    assert not (await observe(c, {}, action="metrics", check_name="http://evil"))["ok"]
    assert not seen
    result = await observe(c, {}, action="metrics", check_name="up", query="DELETE EVERYTHING")
    assert result["ok"] and seen[0].url.params["query"] == "up"
    seen.clear()
    c = record("s", profile="shop-orders")
    assert not (await observe(c, {}, action="check", check_name="http://evil"))["ok"]
    assert not seen
    assert (await observe(c, {}, action="check", check_name="outbox"))["ok"]
    assert seen[0].url.path == "/internal/diagnostics"


def test_exporter_does_not_equate_http_success_with_redis_health():
    assert parse_metrics("# TYPE redis_up gauge\nredis_up 0\n")["redis_up"] == 0
    assert parse_metrics("# empty\n")["empty"]


@pytest.mark.asyncio
async def test_agent_keeps_context_evidence_and_profile_on_model_failure(monkeypatch):
    from infraaxon import agent

    c = record("Worker", profile="shop-worker")
    monkeypatch.setattr(agent, "TOKEN", "scoped")
    monkeypatch.setattr(agent, "spec", AsyncMock(return_value={"component": c, "secrets": {"password": "private"}}))
    monkeypatch.setattr(agent, "completion", AsyncMock(side_effect=RuntimeError("offline")))
    own = {"id": "own", "component_id": "Worker", "action": "inspect", "data": "{}", "ok": True}
    context = {"id": "ctx", "component_id": "Kafka", "action": "inspect", "data": '{"lag":3}', "ok": True}
    answer = await agent.diagnose(
        AgentDiagnosisInput(question="Почему?", initial_evidence=[own], context_evidence=[context]), "Bearer scoped"
    )
    assert answer["agent_profile"] == "shop-worker" and answer["profile_version"] == 2
    assert {e["id"] for e in answer["evidence"]} == {"own", "ctx"} and answer["assessment"] is None
    assert "private" not in json.dumps(agent.completion.call_args.args)


def test_coordinator_retains_baseline_and_context_when_model_fails(client, monkeypatch):
    from infraaxon import api

    eid = env(client)
    logs = component(client, eid, name="Logs", type="elasticsearch", endpoint="http://logs", enabled=True).json()
    target = component(
        client,
        eid,
        name="Worker",
        type="http",
        endpoint="http://worker",
        agent_profile="shop-worker",
        context_sources=[{"component_id": logs["id"], "service_name": "shop-worker"}],
        enabled=True,
    ).json()
    observed = []

    def handle(request):
        data = json.loads(request.content)
        if request.url.path == "/observations":
            observed.append(data)
            return httpx.Response(
                200,
                json={
                    "id": str(len(observed)),
                    "component_id": "source",
                    "source": "http",
                    "action": data.get("action", "inspect"),
                    "check_name": data.get("check_name", ""),
                    "ok": True,
                    "observed_at": "now",
                    "duration_ms": 1,
                    "data": "{}",
                },
            )
        return httpx.Response(503)

    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    job = client.post("/api/components/" + target["id"] + "/diagnoses", json={"question": "worker"}).json()
    import time

    for _ in range(100):
        result = client.get("/api/investigations/" + job["id"]).json()
        if result["status"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    assert result["status"] == "partial"
    assert len(result["results"]) == 2 and len(result["selection"]) == 1
    assert len(result["context_evidence"]) == 1
    assert all(r["evidence"] for r in result["results"])
    assert any(o.get("service_name") == "shop-worker" for o in observed)


def test_kafka_unknown_offset_is_not_reported_as_confirmed_lag(monkeypatch):
    from unittest.mock import MagicMock
    from infraaxon.adapters import _native
    import confluent_kafka
    import confluent_kafka.admin

    admin = MagicMock()
    admin.list_topics.return_value.brokers = {1: object()}
    admin.list_topics.return_value.topics = {"orders.created": MagicMock(partitions={0: object()})}
    consumer = MagicMock()
    consumer.committed.return_value = [confluent_kafka.TopicPartition("orders.created", 0, -1001)]
    consumer.get_watermark_offsets.return_value = (0, 10)
    monkeypatch.setattr(confluent_kafka.admin, "AdminClient", lambda config: admin)
    monkeypatch.setattr(confluent_kafka, "Consumer", lambda config: consumer)
    result = _native(record("Kafka", "kafka", settings={"topic": "orders.created", "group_id": "shop-worker"}), {})
    assert result["lag"] is None and result["unknown_offsets"]
    consumer.subscribe.assert_not_called()
    consumer.commit.assert_not_called()
    consumer.close.assert_called_once()


def test_redis_counter_reset_does_not_create_negative_rates(monkeypatch):
    from unittest.mock import MagicMock
    from infraaxon import adapters
    import redis

    connection = MagicMock()
    values = {"uptime_in_seconds": 100, "keyspace_hits": 100, "keyspace_misses": 10, "evicted_keys": 1}
    connection.__enter__.return_value.info.return_value = values
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kw: connection)
    c = record("redis-counter-test", "redis", endpoint="redis://example")
    adapters._REDIS_SAMPLES.clear()
    adapters._native(c, {})
    connection.__enter__.return_value.info.return_value = {**values, "uptime_in_seconds": 1, "keyspace_hits": 0}
    result = adapters._native(c, {})
    assert "rates_per_second" not in result
    assert "rates_unavailable" in result


def test_cancellation_retains_observations(client, monkeypatch):
    from infraaxon import api
    import time
    import asyncio

    eid = env(client)
    target = component(client, eid, enabled=True).json()

    async def handle(request):
        if request.url.path == "/observations":
            return httpx.Response(
                200,
                json={
                    "id": "persisted",
                    "component_id": target["id"],
                    "source": "redis",
                    "action": "inspect",
                    "ok": True,
                    "observed_at": "now",
                    "duration_ms": 1,
                    "data": "{}",
                },
            )
        await asyncio.sleep(60)
        return httpx.Response(503)

    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    job = client.post("/api/components/" + target["id"] + "/diagnoses", json={"question": "cache"}).json()
    for _ in range(100):
        result = client.get("/api/investigations/" + job["id"]).json()
        if result.get("selection"):
            break
        time.sleep(0.01)
    assert result["results"][0]["evidence"][0]["id"] == "persisted"
    assert client.post("/api/investigations/" + job["id"] + "/cancel").status_code == 200
    result = client.get("/api/investigations/" + job["id"]).json()
    assert result["status"] == "cancelled" and result["results"][0]["evidence"]


@pytest.mark.asyncio
async def test_log_filter_handles_keyword_and_dynamic_mappings(monkeypatch):
    from infraaxon import adapters

    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": []}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        adapters.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    result = await observe(
        record("logs", "elasticsearch"), {}, action="search", service_name="shop-worker", trace_id="abc123"
    )
    assert result["ok"]
    filters = seen[0]["query"]["bool"]["filter"]
    assert {"term": {"service.name.keyword": "shop-worker"}} in filters[1]["bool"]["should"]
    assert {"term": {"trace.id.keyword": "abc123"}} in filters[2]["bool"]["should"]
    assert filters[1]["bool"]["should"][1]["bool"]["must_not"] == [{"exists": {"field": "service.name.keyword"}}]


@pytest.mark.asyncio
async def test_wiki_read_uses_published_route_with_read_only_identity(monkeypatch):
    from infraaxon import adapters

    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == "/graphql":
            assert "single(" not in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "list": [
                                {
                                    "id": 3,
                                    "path": "runbooks/kafka",
                                    "locale": "en",
                                    "title": "Kafka",
                                    "updatedAt": "2026-09-09",
                                    "isPublished": True,
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            text='<html><script>secret layout</script><template slot="contents"><div><p>Check Kafka lag.</p><script>ignore()</script></div></template></html>',
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        adapters.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    result = await observe(record("Wiki", "wikijs"), {"token": "read-key"}, action="read_page", page_id=3)
    assert result["ok"]
    data = json.loads(result["data"])
    assert data["content"] == "Check Kafka lag." and data["updatedAt"] == "2026-09-09"
    assert seen[-1].url.path == "/en/runbooks/kafka"
    assert all(r.headers["Authorization"] == "Bearer read-key" for r in seen)
    assert "read-key" not in result["data"] and "ignore()" not in result["data"]


def test_coordinator_reads_first_linked_wiki_page_without_llm(client, monkeypatch):
    from infraaxon import api
    import time

    eid = env(client)
    wiki = component(client, eid, name="Wiki", type="wikijs", endpoint="http://wiki", enabled=True).json()
    target = component(
        client,
        eid,
        name="Worker",
        type="http",
        endpoint="http://worker",
        agent_profile="shop-worker",
        enabled=True,
        context_sources=[{"component_id": wiki["id"]}],
    ).json()
    calls = []

    def handle(request):
        if request.url.path != "/observations":
            return httpx.Response(503)
        args = json.loads(request.content)
        calls.append(args)
        data = (
            {"data": {"pages": {"search": {"results": [{"id": "3"}]}}}}
            if args.get("action") == "search"
            else {"content": "Runbook content", "updatedAt": "2026-09-10"}
        )
        return httpx.Response(
            200,
            json={
                "id": str(len(calls)),
                "component_id": wiki["id"],
                "source": "wikijs",
                "action": args["action"],
                "ok": True,
                "data": json.dumps(data),
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    job = client.post("/api/components/" + target["id"] + "/diagnoses", json={"question": "Kafka lag"}).json()
    for _ in range(100):
        result = client.get("/api/investigations/" + job["id"]).json()
        if result["status"] not in ("running", "queued"):
            break
        time.sleep(0.01)
    assert result["status"] == "partial"
    assert sum(c["action"] == "read_page" for c in calls) == 1
    assert len(result["context_evidence"]) == 2
    assert any("Runbook content" in e["data"] for e in result["context_evidence"])


@pytest.mark.asyncio
async def test_final_generation_sends_required_assessment_schema(monkeypatch):
    from infraaxon import llm

    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"Observed"}'}}]})

    original = httpx.AsyncClient
    monkeypatch.setenv("LLM_KEY", "test-private")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    await llm.completion([{"role": "user", "content": "test"}])
    schema = seen[0]["response_format"]
    assert schema["type"] == "json_schema"
    assert "summary" in schema["json_schema"]["schema"]["required"]


@pytest.mark.asyncio
async def test_synthesis_repairs_invalid_final_once(monkeypatch):
    from infraaxon import llm

    result = {
        "summary": "Observed",
        "hypotheses": [{"cause": "Read failed", "evidence_ids": ["real"]}],
        "status": "inconclusive",
    }
    complete = AsyncMock(side_effect=[{"content": "{}"}, {"content": json.dumps(result)}])
    monkeypatch.setattr(llm, "completion", complete)
    answer = await llm.synthesize(
        "why", [{"component": "X", "assessment": result, "evidence": [{"id": "real", "data": "{}"}]}]
    )
    assert answer["summary"] == "Observed" and complete.await_count == 2
    assert complete.call_args.kwargs["timeout_seconds"] == 360
