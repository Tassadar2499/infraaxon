"""Bounded, read-only infrastructure tools. No shell or arbitrary SQL execution."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4
import httpx
from .models import TYPES, ObservationInput
from .profiles import resolve, settings_for

_REDIS_SAMPLES = {}


def clean(value, secrets):
    text = json.dumps(value, default=str, ensure_ascii=False)
    for secret in secrets.values():
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r'(?i)(password|authorization|api_key|access_token)(["\s:=]+)[^,\s}\"]+', r"\1\2[REDACTED]", text)
    return text[:24000]


def tool_schema(component=None):
    component = component or {"type": "http"}
    profile = resolve(component)
    properties = {"action": {"type": "string", "enum": profile["actions"]}}
    if "search" in profile["actions"]:
        properties["query"] = {"type": "string", "maxLength": 500}
    if "read_page" in profile["actions"]:
        properties["page_id"] = {"type": "integer", "minimum": 1}
    settings = settings_for(component)
    names = list(dict.fromkeys([*settings.get("checks", {}), *settings.get("queries", {})]))
    if not settings.get("checks") and "check" in properties["action"]["enum"]:
        properties["action"]["enum"] = [a for a in properties["action"]["enum"] if a != "check"]
    if names:
        properties["check_name"] = {"type": "string", "enum": names}
    return {
        "type": "function",
        "function": {
            "name": "inspect_component",
            "description": "Read THIS component only. Choose a supported action and named check; never supply URLs, SQL or PromQL. read_page uses an ID returned by search.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


async def observe(
    component,
    secrets,
    action="inspect",
    query="",
    page_id=None,
    window=15,
    trace_id=None,
    check_name="",
    service_name="",
):
    started = time.monotonic()
    eid = str(uuid4())
    try:
        ObservationInput(
            action=action,
            query=query,
            page_id=page_id,
            time_window_minutes=window,
            trace_id=trace_id,
            check_name=check_name,
            service_name=service_name,
        )
        if action not in resolve(component)["actions"]:
            raise ValueError("Unsupported read action")
        if len(query) > 500:
            raise ValueError("Query too long")
        data = await asyncio.wait_for(
            _read(component, secrets, action, query, page_id, window, trace_id, check_name, service_name), 25
        )
        ok = not (
            isinstance(data, dict)
            and (
                data.get("healthy") is False
                or data.get("status") in ("error", "degraded", "unhealthy")
                or data.get("redis_up") == 0
            )
        )
    except Exception as exc:
        data = {"error": type(exc).__name__, "message": str(exc)}
        ok = False
    return {
        "id": eid,
        "component_id": component["id"],
        "source": component["type"],
        "action": action,
        "check_name": check_name,
        "service_name": service_name,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "data": clean(data, secrets),
    }


async def _read(c, s, action, query, page_id, window, trace_id, check_name="", service_name=""):
    kind, endpoint, settings = c["type"], c["endpoint"].rstrip("/"), settings_for(c)
    if action == "check" and check_name in settings.get("check_ports", {}):
        url = urlsplit(endpoint)
        port = int(settings["check_ports"][check_name])
        if not 1 <= port <= 65535:
            raise ValueError("Invalid check port")
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        endpoint = urlunsplit((url.scheme, f"{host}:{port}", url.path, "", ""))
    if kind not in TYPES:
        raise ValueError("Unknown adapter")
    if kind in {"mongodb", "redis", "postgresql", "s3", "kafka"}:
        if action != "inspect":
            raise ValueError("Only inspect is supported by this adapter")
        return await asyncio.to_thread(_native, c, s)
    headers = {}
    if s.get("token"):
        headers["Authorization"] = "Bearer " + s["token"]
    auth = (s["username"], s.get("password", "")) if s.get("username") else None
    if kind == "openproject" and s.get("token"):
        import base64

        headers["Authorization"] = "Basic " + base64.b64encode(("apikey:" + s["token"]).encode()).decode()
    async with httpx.AsyncClient(timeout=10, headers=headers, auth=auth, follow_redirects=False) as client:

        async def get(path, params=None):
            response = await client.get(endpoint + path, params=params)
            response.raise_for_status()
            return (
                response.json()
                if "json" in response.headers.get("content-type", "")
                else response.text[: 240000 if path == "/metrics" else 12000]
            )

        if action == "check" or kind == "http":
            path = (
                settings.get("health_path", "/health")
                if action == "inspect"
                else settings.get("checks", {}).get(check_name)
            )
            if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
                raise ValueError("Unknown or invalid named HTTP check")
            if c.get("agent_profile") == "toxiproxy" and path != "/version":
                raise ValueError("Only version is available for Toxiproxy")
            data = await get(path)
            if check_name == "assets" and isinstance(data, str):
                paths = list(dict.fromkeys(re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)', data)))[:6]
                assets = []
                for asset in paths:
                    response = await client.get(endpoint + asset)
                    assets.append(
                        {
                            "path": asset,
                            "status": response.status_code,
                            "content_type": response.headers.get("content-type", ""),
                        }
                    )
                return {
                    "assets": assets,
                    "healthy": bool(assets)
                    and all(a["status"] == 200 and "text/html" not in a["content_type"] for a in assets),
                }
            if isinstance(data, str) and (path == "/metrics" or check_name == "metrics"):
                return parse_metrics(data)
            return data
        if kind == "prometheus":
            if action == "targets":
                data = await get("/api/v1/targets", {"state": "active"})
                return {
                    "targets": [
                        {k: t.get(k) for k in ("labels", "health", "lastScrape", "lastError")}
                        for t in data.get("data", {}).get("activeTargets", [])[:50]
                    ]
                }
            name = check_name or "up"
            expression = settings.get("queries", {}).get(name)
            if not expression:
                raise ValueError("Unknown named metrics query")
            data = await get("/api/v1/query", {"query": expression, "timeout": "5s"})
            rows = data.get("data", {}).get("result", [])
            return {"query_name": name, "status": data.get("status"), "result": rows[:50], "empty": not rows}
        if kind == "grafana":
            return await get("/api/health")
        if kind == "kibana":
            return await get("/api/status")
        if kind == "elasticsearch":
            if action == "inspect":
                return await get("/_cluster/health")
            filters = [{"range": {"@timestamp": {"gte": f"now-{window}m"}}}]
            if service_name or settings.get("service_name"):
                filters.append(exact_filter("service.name", service_name or settings["service_name"]))
            if trace_id:
                filters.append(exact_filter("trace.id", trace_id))
            body = {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {"filter": filters, "must": [{"simple_query_string": {"query": query}}] if query else []}
                },
            }
            index = quote(settings.get("index", "infraaxon-*"), safe="*,-_")
            response = await client.post(endpoint + f"/{index}/_search", json=body)
            response.raise_for_status()
            data = response.json()
            return {
                "total": data.get("hits", {}).get("total"),
                "records": data.get("hits", {}).get("hits", []),
                "window_minutes": window,
            }
        if kind == "openproject":
            if action == "read_page":
                if not page_id:
                    raise ValueError("page_id required")
                work = await get(f"/api/v3/work_packages/{page_id}")
                project = str(work.get("_links", {}).get("project", {}).get("href", "").rsplit("/", 1)[-1])
                if settings.get("project_id") and project != str(settings["project_id"]):
                    raise ValueError("Work package is outside configured project")
                activities = await get(f"/api/v3/work_packages/{page_id}/activities", {"pageSize": 20})
                return {
                    "work_package": {
                        k: work.get(k) for k in ("id", "subject", "description", "createdAt", "updatedAt")
                    },
                    "activities": [
                        {k: a.get(k) for k in ("id", "comment", "details", "createdAt")}
                        for a in activities.get("_embedded", {}).get("elements", [])[:20]
                    ],
                }
            if action == "inspect":
                data = await get("/api/v3/projects", {"pageSize": 20})
                return {
                    "projects": [
                        {k: p.get(k) for k in ("id", "name", "identifier", "updatedAt")}
                        for p in data.get("_embedded", {}).get("elements", [])
                    ]
                }
            filters = []
            if settings.get("project_id"):
                filters.append({"project": {"operator": "=", "values": [str(settings["project_id"])]}})
            if query:
                filters.append({"subject": {"operator": "~", "values": [query]}})
            data = await get("/api/v3/work_packages", {"pageSize": 20, "filters": json.dumps(filters)})
            return {
                "total": data.get("total"),
                "work_packages": [
                    {k: p.get(k) for k in ("id", "subject", "description", "createdAt", "updatedAt")}
                    for p in data.get("_embedded", {}).get("elements", [])
                ],
            }
        if kind == "wikijs":
            if action == "read_page":
                if not page_id:
                    raise ValueError("page_id required")
                # Wiki.js 2.5 single/singleByPath checks manage:pages internally,
                # even though its GraphQL declaration says read:pages. Use the
                # published page route with the existing read-only identity.
                response = await client.post(
                    endpoint + "/graphql",
                    json={"query": "{pages{list(limit:1000){id path locale title updatedAt isPublished}}}"},
                )
                response.raise_for_status()
                listing = response.json()
                if listing.get("errors"):
                    raise ValueError("Page metadata unavailable to reader")
                page = next(
                    (
                        p
                        for p in listing.get("data", {}).get("pages", {}).get("list", [])
                        if p["id"] == page_id and p["isPublished"]
                    ),
                    None,
                )
                if not page:
                    raise ValueError("Published page not found in bounded reader-visible list")
                path = "/" + quote(page["locale"], safe="") + "/" + quote(page["path"], safe="/")
                response = await client.get(endpoint + path)
                response.raise_for_status()
                content = published_wiki_text(response.text[:1000000])
                if not content:
                    raise ValueError("Published page content unavailable to reader")
                return {
                    **page,
                    "url": endpoint + path,
                    "content": content[:16000],
                    "content_truncated": len(content) > 16000,
                }
            elif action == "search":
                gql = "query($q:String!){pages{search(query:$q){results{id path title description}}}}"
                variables = {"q": query or settings.get("search", "infrastructure")}
            else:
                gql = "{pages{list(limit:20){id path title}}}"
                variables = {}
            response = await client.post(endpoint + "/graphql", json={"query": gql, "variables": variables})
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                raise ValueError(json.dumps(result["errors"]))
            return result
        if kind == "mattermost":
            if action == "inspect":
                return await get("/api/v4/system/ping")
            team = settings.get("team_id")
            if not team:
                raise ValueError("Configure team_id for Mattermost search")
            terms = query
            if settings.get("channel_name"):
                terms += " in:" + settings["channel_name"]
            response = await client.post(
                endpoint + f"/api/v4/teams/{quote(team, safe='')}/posts/search",
                json={"terms": terms, "is_or_search": False},
            )
            response.raise_for_status()
            data = response.json()
            return {
                "posts": [
                    {k: p.get(k) for k in ("id", "channel_id", "message", "create_at", "update_at")}
                    for p in list(data.get("posts", {}).values())[:20]
                ],
                "total_count": data.get("total_count"),
            }
    raise ValueError("Unsupported operation")


def _native(c, s):
    kind, endpoint, cfg = c["type"], c["endpoint"], settings_for(c)
    if kind == "mongodb":
        from pymongo import MongoClient

        kwargs = {"username": s["username"], "password": s.get("password", "")} if s.get("username") else {}
        with MongoClient(endpoint, serverSelectionTimeoutMS=5000, socketTimeoutMS=5000, **kwargs) as client:
            out = {"ping": client.admin.command("ping")}
            try:
                status = client.admin.command("serverStatus")
                out["statistics"] = {k: status.get(k) for k in ("connections", "opcounters", "mem", "uptime")}
                out["operations"] = client.admin.command("currentOp", active=True).get("inprog", [])[:10]
            except Exception as exc:
                out["restricted_statistics"] = str(exc)
            return out
    if kind == "redis":
        import redis

        with redis.Redis.from_url(
            endpoint,
            password=s.get("password") or None,
            socket_timeout=5,
            socket_connect_timeout=5,
            decode_responses=True,
        ) as client:
            info = client.info()
            result = {
                k: info.get(k)
                for k in (
                    "redis_version",
                    "connected_clients",
                    "used_memory_human",
                    "used_memory",
                    "maxmemory",
                    "keyspace_hits",
                    "keyspace_misses",
                    "evicted_keys",
                    "uptime_in_seconds",
                )
            }
            sample_time = time.monotonic()
            previous = _REDIS_SAMPLES.get(c["id"])
            if previous:
                old_time, old = previous
                keys = ("keyspace_hits", "keyspace_misses", "evicted_keys")
                elapsed = sample_time - old_time
                stable = result["uptime_in_seconds"] >= old["uptime_in_seconds"] + max(0, elapsed - 2)
                if stable and elapsed >= 1 and all(result[k] >= old[k] for k in keys):
                    result["rates_per_second"] = {k: round((result[k] - old[k]) / elapsed, 4) for k in keys}
                    result["sample_seconds"] = round(elapsed, 2)
                else:
                    result["rates_unavailable"] = "Restart, reset or insufficient sample interval"
            _REDIS_SAMPLES[c["id"]] = (sample_time, result)
            return result
    if kind == "postgresql":
        import psycopg

        kwargs = {"user": s["username"], "password": s.get("password", "")} if s.get("username") else {}
        with psycopg.connect(
            endpoint,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c default_transaction_read_only=on",
            **kwargs,
        ) as conn:
            return {
                "connections": conn.execute(
                    "SELECT datname,state,count(*) FROM pg_stat_activity GROUP BY datname,state"
                ).fetchall(),
                "long_transactions": conn.execute(
                    "SELECT pid,datname,state,extract(epoch FROM now()-xact_start)::int AS age_seconds FROM pg_stat_activity WHERE xact_start < now()-interval '30 seconds' AND pid<>pg_backend_pid() ORDER BY xact_start LIMIT 20"
                ).fetchall(),
                "blocked": conn.execute(
                    "SELECT pid,datname,pg_blocking_pids(pid) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0 LIMIT 20"
                ).fetchall(),
            }
    if kind == "s3":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=s.get("access_key"),
            aws_secret_access_key=s.get("secret_key"),
            region_name=cfg.get("region", "us-east-1"),
            config=Config(
                connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}, s3={"addressing_style": "path"}
            ),
        )
        bucket = cfg.get("bucket")
        if not bucket:
            raise ValueError("Configure a bucket; global bucket enumeration is not used")
        client.head_bucket(Bucket=bucket)
        out = {"bucket": bucket, "reachable": True}
        if cfg.get("object_key"):
            meta = client.head_object(Bucket=bucket, Key=cfg["object_key"])
            out["object"] = {"key": cfg["object_key"], "bytes": meta["ContentLength"]}
        client.close()
        return out
    if kind == "kafka":
        from confluent_kafka import Consumer, TopicPartition
        from confluent_kafka.admin import AdminClient

        config = {"bootstrap.servers": endpoint, "socket.timeout.ms": 5000}
        if s.get("username"):
            config.update(
                {
                    "security.protocol": cfg.get("security_protocol", "SASL_SSL"),
                    "sasl.mechanism": cfg.get("sasl_mechanism", "PLAIN"),
                    "sasl.username": s["username"],
                    "sasl.password": s.get("password", ""),
                }
            )
        admin = AdminClient(config)
        metadata = admin.list_topics(timeout=5)
        result = {"brokers": list(metadata.brokers), "topics": list(metadata.topics)[:50]}
        topic, group = cfg.get("topic"), cfg.get("group_id")
        if topic and group and topic in metadata.topics:
            consumer = Consumer({**config, "group.id": group, "enable.auto.commit": False})
            try:
                partitions = [TopicPartition(topic, p) for p in metadata.topics[topic].partitions]
                committed = consumer.committed(partitions, timeout=5)
                result["partitions"] = []
                for p in committed:
                    low, high = consumer.get_watermark_offsets(p, timeout=5)
                    result["partitions"].append(
                        {
                            "partition": p.partition,
                            "committed": p.offset,
                            "high": high,
                            "low": low,
                            "lag": max(0, high - p.offset) if low <= p.offset <= high else None,
                        }
                    )
                result["lag"] = (
                    sum(p["lag"] for p in result["partitions"])
                    if all(p["lag"] is not None for p in result["partitions"])
                    else None
                )
                result["unknown_offsets"] = any(p["lag"] is None for p in result["partitions"])
            finally:
                consumer.close()
        return result


def parse_metrics(text):
    from prometheus_client.parser import text_string_to_metric_families

    prefixes = (
        "redis_up",
        "redis_exporter_",
        "kafka_brokers",
        "kafka_topic_",
        "kafka_consumergroup_",
        "otelcol_receiver_",
        "otelcol_exporter_",
        "otelcol_processor_",
        "process_start_time_seconds",
    )
    samples = []
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name.startswith(prefixes):
                samples.append({"name": sample.name, "labels": sample.labels, "value": sample.value})
            if len(samples) >= 100:
                break
        if len(samples) >= 100:
            break
    result = {"samples": samples, "empty": not samples}
    for sample in samples:
        if sample["name"] == "redis_up":
            result["redis_up"] = sample["value"]
    return result


def exact_filter(field, value):
    """Support both ECS keyword mappings and pre-existing dynamic text + keyword mappings."""
    return {
        "bool": {
            "minimum_should_match": 1,
            "should": [
                {"term": {field + ".keyword": value}},
                {"bool": {"must": [{"term": {field: value}}], "must_not": [{"exists": {"field": field + ".keyword"}}]}},
            ],
        }
    }


def published_wiki_text(html):
    from html.parser import HTMLParser

    match = re.search(r'<template\s+slot="contents">(.*?)</template>', html, re.S)
    if not match:
        return ""

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.hidden = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style"}:
                self.hidden += 1
            if tag in {"p", "div", "li", "br", "h1", "h2", "h3"}:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in {"script", "style"}:
                self.hidden = max(0, self.hidden - 1)

        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)

    reader = Text()
    reader.feed(match.group(1))
    return "".join(reader.parts).strip()
