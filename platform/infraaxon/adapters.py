"""Bounded, read-only infrastructure tools. No shell or arbitrary SQL execution."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4
import httpx
from .models import TYPES


def clean(value, secrets):
    text = json.dumps(value, default=str, ensure_ascii=False)
    for secret in secrets.values():
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r'(?i)(password|authorization|api_key|access_token)(["\s:=]+)[^,\s}\"]+', r"\1\2[REDACTED]", text)
    return text[:24000]


def tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "inspect_component",
            "description": "Read observations from THIS component only. inspect: health/statistics; search: logs/docs; read_page: Wiki.js page; metrics: Prometheus query configured by operator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["inspect", "search", "read_page", "metrics"]},
                    "query": {"type": "string", "maxLength": 500},
                    "page_id": {"type": "integer"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


async def observe(component, secrets, action="inspect", query="", page_id=None, window=15, trace_id=None):
    started = time.monotonic()
    eid = str(uuid4())
    try:
        if action not in {"inspect", "search", "read_page", "metrics"}:
            raise ValueError("Unsupported read action")
        if len(query) > 500:
            raise ValueError("Query too long")
        data = await asyncio.wait_for(_read(component, secrets, action, query, page_id, window, trace_id), 25)
        ok = True
    except Exception as exc:
        data = {"error": type(exc).__name__, "message": str(exc)}
        ok = False
    return {
        "id": eid,
        "component_id": component["id"],
        "source": component["type"],
        "action": action,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "data": clean(data, secrets),
    }


async def _read(c, s, action, query, page_id, window, trace_id):
    kind, endpoint, settings = c["type"], c["endpoint"].rstrip("/"), c.get("settings", {})
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
            return response.json() if "json" in response.headers.get("content-type", "") else response.text[:6000]

        if kind == "http":
            if action != "inspect":
                raise ValueError("Use a telemetry component for log searches")
            return await get(settings.get("health_path", "/health"))
        if kind == "prometheus":
            return await get("/api/v1/query", {"query": settings.get("query", "up"), "timeout": "5s"})
        if kind == "grafana":
            return await get("/api/health")
        if kind == "kibana":
            return await get("/api/status")
        if kind == "elasticsearch":
            if action == "inspect":
                return await get("/_cluster/health")
            filters = [{"range": {"@timestamp": {"gte": f"now-{window}m"}}}]
            if settings.get("service_name"):
                filters.append({"term": {"service.name": settings["service_name"]}})
            if trace_id:
                filters.append({"term": {"trace.id": trace_id}})
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
            return response.json()
        if kind == "openproject":
            if action == "inspect":
                return await get("/api/v3/projects", {"pageSize": 20})
            filters = []
            if settings.get("project_id"):
                filters.append({"project": {"operator": "=", "values": [str(settings["project_id"])]}})
            if query:
                filters.append({"subject": {"operator": "~", "values": [query]}})
            return await get("/api/v3/work_packages", {"pageSize": 20, "filters": json.dumps(filters)})
        if kind == "wikijs":
            if action == "read_page":
                if not page_id:
                    raise ValueError("page_id required")
                gql = "query($id:Int!){pages{single(id:$id){id path title content updatedAt}}}"
                variables = {"id": page_id}
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
            return response.json()
    raise ValueError("Unsupported operation")


def _native(c, s):
    kind, endpoint, cfg = c["type"], c["endpoint"], c.get("settings", {})
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
            return {
                k: info.get(k)
                for k in (
                    "redis_version",
                    "connected_clients",
                    "used_memory_human",
                    "keyspace_hits",
                    "keyspace_misses",
                    "evicted_keys",
                    "uptime_in_seconds",
                )
            }
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
                ).fetchall()
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
                            "lag": max(0, high - max(low, p.offset)),
                        }
                    )
                result["lag"] = sum(p["lag"] for p in result["partitions"])
            finally:
                consumer.close()
        return result
