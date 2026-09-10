import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from .models import ComponentInput, EnvironmentInput, DiagnosisInput, TYPES, AgentProfile, ObservationInput
from .store import configured_store, public_component
from .adapters import observe
from .llm import synthesize
from .profiles import all_profiles, resolve
from .routing import rank_components, context_requests

DIAGNOSES = Counter("infraaxon_investigations_total", "Investigations started")


def now():
    return datetime.now(timezone.utc).isoformat()


def create_app(store=None):
    tasks = {}
    inference_lock = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store or configured_store()
        for job in app.state.store.all("investigation"):
            if job["status"] in {"queued", "running"}:
                job.update(status="interrupted", finished_at=now())
                app.state.store.put("investigation", job)
        yield
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*list(tasks.values()), return_exceptions=True)
        if store is None:
            app.state.store.close()

    app = FastAPI(title="InfraAxon Platform", version="0.1.0", lifespan=lifespan)

    def db():
        return app.state.store

    def authorize(request: Request):
        expected = os.environ.get("PLATFORM_KEY", "")
        actual = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get(
            "infraaxon_session", ""
        )
        if not expected or not secrets.compare_digest(actual, expected):
            raise HTTPException(401, "Authentication required")

    def component(id):
        c = db().get("component", id)
        if not c:
            raise HTTPException(404, "Component not found")
        return c

    def environment(id):
        e = db().get("environment", id)
        if not e:
            raise HTTPException(404, "Environment not found")
        return e

    def agent_auth(id, request):
        c = component(id)
        token = db().decrypt(c["encrypted_secrets"])["_agent_token"]
        if not c["enabled"] or not secrets.compare_digest(request.headers.get("x-agent-token", ""), token):
            raise HTTPException(401, "Invalid agent identity")
        return c

    class Login(BaseModel):
        token: str

    @app.post("/api/login")
    def login(body: Login, response: Response):
        expected = os.environ.get("PLATFORM_KEY", "")
        if not expected or not secrets.compare_digest(body.token, expected):
            raise HTTPException(401, "Invalid operator key")
        response.set_cookie("infraaxon_session", body.token, httponly=True, samesite="strict", max_age=28800)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("infraaxon_session")
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok", "product": "InfraAxon"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/internal/diagnostics")
    def diagnostics(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        allowed = any(
            c.get("enabled")
            and resolve(c)["id"] == "platform"
            and secrets.compare_digest(token, db().decrypt(c["encrypted_secrets"])["_agent_token"])
            for c in db().all("component")
        )
        if not allowed:
            raise HTTPException(401)
        jobs, components = db().all("investigation"), db().all("component")
        return {
            "sqlite": "readable",
            "jobs": {
                state: sum(j["status"] == state for j in jobs) for state in ("queued", "running", "failed", "partial")
            },
            "agents": [
                {
                    "name": c["name"],
                    "enabled": c["enabled"],
                    "last_seen": c.get("last_seen"),
                    "state": c.get("agent_state"),
                }
                for c in components
            ],
        }

    @app.get("/api/agent-profiles", dependencies=[Depends(authorize)])
    def agent_profiles() -> list[AgentProfile]:
        return list(all_profiles().values())

    @app.get("/api/adapter-types", dependencies=[Depends(authorize)])
    def adapters():
        settings = {
            "http": {"health_path": "/health", "service_name": ""},
            "s3": {"bucket": "", "object_key": ""},
            "kafka": {"topic": "", "group_id": ""},
            "openproject": {"project_id": ""},
            "wikijs": {"search": "infrastructure"},
            "mattermost": {"team_id": "", "channel_name": ""},
            "elasticsearch": {"index": "infraaxon-*", "service_name": ""},
            "prometheus": {"query": "up"},
        }
        secret_fields = {
            "mongodb": ["username", "password"],
            "redis": ["password"],
            "s3": ["access_key", "secret_key"],
            "postgresql": ["username", "password"],
            "kafka": ["username", "password"],
        }
        return [
            {"type": k, "name": v, "settings": settings.get(k, {}), "secret_fields": secret_fields.get(k, ["token"])}
            for k, v in TYPES.items()
        ]

    @app.get("/api/environments", dependencies=[Depends(authorize)])
    def environments():
        return db().all("environment")

    @app.post("/api/environments", status_code=201, dependencies=[Depends(authorize)])
    def add_environment(body: EnvironmentInput):
        return db().put("environment", {"id": str(uuid4()), **body.model_dump(), "created_at": now()})

    @app.get("/api/environments/{id}/components", dependencies=[Depends(authorize)])
    def components(id: str):
        environment(id)
        return [public_component(c) for c in db().all("component") if c["environment_id"] == id]

    def save_component(env_id, body, existing=None):
        environment(env_id)
        id = existing["id"] if existing else str(uuid4())
        for dependency in body.dependencies:
            target = component(dependency)
            if dependency == id or target["environment_id"] != env_id:
                raise HTTPException(422, "Dependencies must be other components in the same environment")
        if (
            existing
            and existing["type"] != body.type
            and body.type not in {"elasticsearch", "prometheus", "wikijs", "openproject", "mattermost"}
        ):
            if any(
                any(link["component_id"] == id for link in other.get("context_sources", []))
                for other in db().all("component")
            ):
                raise HTTPException(422, "Component is used as a context source")
        for source in body.context_sources:
            target = component(source.component_id)
            if source.component_id == id or target["environment_id"] != env_id:
                raise HTTPException(422, "Context sources must be other components in the same environment")
            if target["type"] not in {"elasticsearch", "prometheus", "wikijs", "openproject", "mattermost"}:
                raise HTTPException(422, "Unsupported context source type")
        credentials = (
            db().decrypt(existing["encrypted_secrets"]) if existing else {"_agent_token": secrets.token_urlsafe(32)}
        )
        credentials.update(body.secrets)
        fields = body.model_dump(exclude={"secrets"})
        for key in ("agent_profile", "context_sources"):
            if existing and key not in body.model_fields_set:
                fields[key] = existing.get(key, "" if key == "agent_profile" else [])
        if existing and existing["type"] != body.type and "agent_profile" not in body.model_fields_set:
            fields["agent_profile"] = body.type
        fields["agent_profile"] = fields.get("agent_profile") or body.type
        if existing and "web_url" not in body.model_fields_set:
            fields["web_url"] = existing.get("web_url", "")
        c = {
            "id": id,
            "environment_id": env_id,
            **fields,
            "encrypted_secrets": db().encrypt(credentials),
            "updated_at": now(),
            "agent_state": "pending" if body.enabled else "disabled",
        }
        return public_component(db().put("component", c))

    @app.post("/api/environments/{id}/components", status_code=201, dependencies=[Depends(authorize)])
    def add_component(id: str, body: ComponentInput):
        return save_component(id, body)

    @app.put("/api/components/{id}", dependencies=[Depends(authorize)])
    def update_component(id: str, body: ComponentInput):
        c = component(id)
        return save_component(c["environment_id"], body, c)

    @app.delete("/api/components/{id}", dependencies=[Depends(authorize)])
    def delete_component(id: str):
        component(id)
        db().delete("component", id)
        for c in db().all("component"):
            if id in c["dependencies"]:
                c["dependencies"].remove(id)
                db().put("component", c)
            if any(link["component_id"] == id for link in c.get("context_sources", [])):
                c["context_sources"] = [link for link in c["context_sources"] if link["component_id"] != id]
                db().put("component", c)
        return {"ok": True}

    @app.post("/api/components/{id}/check", dependencies=[Depends(authorize)])
    async def check(id: str):
        c = component(id)
        observation = await observe(c, db().decrypt(c["encrypted_secrets"]))
        current = component(id)
        current["observation"] = observation
        db().put("component", current)
        return observation

    @app.get("/internal/desired", dependencies=[Depends(authorize)])
    def desired():
        return [
            {"id": c["id"], "revision": c["updated_at"], "token": db().decrypt(c["encrypted_secrets"])["_agent_token"]}
            for c in db().all("component")
            if c["enabled"]
        ]

    @app.get("/internal/components/{id}/config")
    def config(id: str, request: Request):
        c = agent_auth(id, request)
        return {
            "component": public_component(c),
            "secrets": {k: v for k, v in db().decrypt(c["encrypted_secrets"]).items() if not k.startswith("_")},
        }

    @app.post("/internal/components/{id}/report")
    async def report(id: str, request: Request):
        c = agent_auth(id, request)
        data = await request.json()
        c = component(id)
        c.update(agent_state="running", last_seen=now(), observation=data["observation"])
        db().put("component", c)
        return {"ok": True}

    def select(env_id, body):
        available = [c for c in db().all("component") if c["environment_id"] == env_id and c["enabled"]]
        if body.component_id:
            target = component(body.component_id)
            if target["environment_id"] != env_id or not target["enabled"]:
                raise HTTPException(422, "Target must be enabled in this environment")
        return available

    async def run_job(id, available, body, direct):
        def update(**fields):
            job = db().get("investigation", id)
            job.update(fields)
            db().put("investigation", job)
            return job

        results = []
        limiter = asyncio.Semaphore(4)
        baseline = {}
        by_id = {}
        incomplete = []
        all_context = {}
        observation_cache = {}

        async def observation(client, c, args):
            normalized = ObservationInput(**args).model_dump()
            key = (c["id"], json.dumps(normalized, sort_keys=True))
            if key not in observation_cache:
                observation_cache[key] = asyncio.create_task(read_observation(client, c, normalized))
            return await observation_cache[key]

        async def read_observation(client, c, args):
            async with limiter:
                try:
                    token = db().decrypt(component(c["id"])["encrypted_secrets"])["_agent_token"]
                    async with asyncio.timeout(25):
                        response = await client.post(
                            f"http://infraaxon-agent-{c['id']}:8000/observations",
                            headers={"Authorization": "Bearer " + token},
                            json=args,
                        )
                        response.raise_for_status()
                        return response.json()
                except Exception as exc:
                    return {
                        "id": str(uuid4()),
                        "component_id": c["id"],
                        "source": c["type"],
                        "action": args.get("action", "inspect"),
                        "check_name": args.get("check_name", ""),
                        "observed_at": now(),
                        "ok": False,
                        "duration_ms": 0,
                        "data": json.dumps({"error": type(exc).__name__, "message": "Agent observation unavailable"}),
                    }

        async def base_probe(client, c):
            ev = await observation(
                client, c, {"time_window_minutes": body.time_window_minutes, "trace_id": body.trace_id}
            )
            baseline[c["id"]] = ev
            p = resolve(c)
            record = {
                "component_id": c["id"],
                "component": c["name"],
                "agent_profile": p["id"],
                "profile_version": p["version"],
                "assessment": None,
                "evidence": [ev],
                "selected": False,
            }
            by_id[c["id"]] = record
            results.append(record)
            update(results=results)

        try:
            async with inference_lock:
                update(status="running", started_at=now(), progress="Сбор наблюдений без модели")
                async with asyncio.timeout(1200), httpx.AsyncClient(timeout=httpx.Timeout(210, connect=5)) as client:
                    # TaskGroup cancels outstanding probes before partial history is finalized.
                    async with asyncio.TaskGroup() as probes:
                        for c in available:
                            probes.create_task(base_probe(client, c))
                    ranked, skipped = rank_components(available, body.question, body.component_id, baseline, direct)
                    chosen = [c for c, _ in ranked]
                    for c, reason in ranked:
                        by_id[c["id"]].update(selected=True, selection_reason=reason)
                    update(
                        results=results,
                        selection=[
                            {"component_id": c["id"], "component": c["name"], "reason": reason} for c, reason in ranked
                        ],
                        skipped_specialists=skipped,
                        progress="Сбор профильных проверок и контекста",
                    )
                    requests, skipped_sources = context_requests(
                        chosen, db().all("component"), body.question, body.time_window_minutes, body.trace_id
                    )
                    update(skipped_sources=skipped_sources)
                    context_by_target = {c["id"]: [] for c in chosen}

                    async def context_probe(item):
                        async def retain(ev):
                            all_context[ev["id"]] = ev
                            for target in item["targets"]:
                                if not any(old["id"] == ev["id"] for old in context_by_target[target]):
                                    context_by_target[target].append(ev)
                            if not ev["ok"]:
                                incomplete.append(
                                    {
                                        "component_id": item["source"]["id"],
                                        "reason": "Ошибка получения контекста",
                                        "evidence_id": ev["id"],
                                    }
                                )
                            update(context_evidence=list(all_context.values()), incomplete_reasons=incomplete)

                        ev = await observation(client, item["source"], item["args"])
                        await retain(ev)
                        if item["source"]["type"] == "wikijs" and ev["ok"]:
                            try:
                                pages = (
                                    json.loads(ev["data"])
                                    .get("data", {})
                                    .get("pages", {})
                                    .get("search", {})
                                    .get("results", [])
                                )
                                if pages:
                                    page = await observation(
                                        client,
                                        item["source"],
                                        {
                                            "action": "read_page",
                                            "page_id": int(pages[0]["id"]),
                                            "time_window_minutes": body.time_window_minutes,
                                        },
                                    )
                                    await retain(page)
                            except (ValueError, KeyError, TypeError):
                                incomplete.append(
                                    {
                                        "component_id": item["source"]["id"],
                                        "reason": "Не удалось извлечь страницу из результата поиска",
                                    }
                                )
                                update(incomplete_reasons=incomplete)

                    async def required_probes(c):
                        for check in resolve(c)["initial_checks"]:
                            if check["action"] == "inspect":
                                continue
                            ev = await observation(
                                client,
                                c,
                                {**check, "time_window_minutes": body.time_window_minutes, "trace_id": body.trace_id},
                            )
                            by_id[c["id"]]["evidence"].append(ev)
                            update(results=results)

                    async with asyncio.TaskGroup() as probes:
                        for item in requests:
                            probes.create_task(context_probe(item))
                        for c in chosen:
                            probes.create_task(required_probes(c))
                    for c in chosen:
                        record = by_id[c["id"]]
                        own_evidence = list(record["evidence"])
                        record["evidence"] += context_by_target[c["id"]]
                        update(results=results, progress=f"Диагностика: {c['name']}")
                        try:
                            token = db().decrypt(component(c["id"])["encrypted_secrets"])["_agent_token"]
                            response = await client.post(
                                f"http://infraaxon-agent-{c['id']}:8000/diagnoses",
                                headers={"Authorization": "Bearer " + token},
                                json={
                                    **body.model_dump(),
                                    "initial_evidence": own_evidence,
                                    "context_evidence": context_by_target[c["id"]],
                                },
                            )
                            response.raise_for_status()
                            answer = response.json()
                            record.update(answer)
                        except Exception as exc:
                            record["error"] = "Agent diagnosis unavailable: " + type(exc).__name__
                        update(results=results)
                    specialists = [by_id[c["id"]] for c in chosen]
                    assessment = None
                    if direct:
                        assessment = specialists[0].get("assessment")
                    elif any(r.get("assessment") for r in specialists):
                        update(progress="Сопоставление свидетельств")
                        names = {c["id"]: c["name"] for c in available}
                        topology = [
                            {"component": c["name"], "depends_on": [names.get(d, d) for d in c["dependencies"]]}
                            for c in chosen
                        ]
                        assessment = await synthesize(body.question, specialists, topology)
                    for record in specialists:
                        if record.get("error"):
                            incomplete.append({"component_id": record["component_id"], "reason": record["error"]})
                        for ev in record["evidence"]:
                            if not ev["ok"]:
                                incomplete.append(
                                    {
                                        "component_id": ev["component_id"],
                                        "reason": "Неуспешное наблюдение",
                                        "evidence_id": ev["id"],
                                    }
                                )
                    incomplete.extend(skipped_sources)
                    update(
                        status="completed" if assessment and not incomplete else "partial",
                        assessment=assessment,
                        results=results,
                        incomplete_reasons=incomplete,
                        finished_at=now(),
                        progress="Готово",
                    )
        except asyncio.CancelledError:
            update(status="cancelled", finished_at=now(), results=results, context_evidence=list(all_context.values()))
            raise
        except Exception as exc:
            update(
                status="partial" if results else "failed",
                error=type(exc).__name__,
                results=results,
                context_evidence=list(all_context.values()),
                finished_at=now(),
            )
        finally:
            tasks.pop(id, None)

    def start(env_id, body, direct=False):
        environment(env_id)
        chosen = select(env_id, body)
        if not chosen:
            raise HTTPException(422, "Activate at least one agent")
        id = str(uuid4())
        job = db().put(
            "investigation",
            {
                "id": id,
                "environment_id": env_id,
                "request": body.model_dump(),
                "status": "queued",
                "created_at": now(),
                "results": [],
                "progress": "В очереди",
            },
        )
        DIAGNOSES.inc()
        tasks[id] = asyncio.create_task(run_job(id, chosen, body, direct))
        return job

    @app.post("/api/environments/{id}/investigations", status_code=202, dependencies=[Depends(authorize)])
    async def investigate(id: str, body: DiagnosisInput):
        return start(id, body)

    @app.post("/api/components/{id}/diagnoses", status_code=202, dependencies=[Depends(authorize)])
    async def diagnose(id: str, body: DiagnosisInput):
        c = component(id)
        body.component_id = id
        return start(c["environment_id"], body, True)

    @app.get("/api/environments/{id}/investigations", dependencies=[Depends(authorize)])
    def history(id: str):
        environment(id)
        return [j for j in db().all("investigation") if j["environment_id"] == id][::-1]

    def job_by_id(id):
        job = db().get("investigation", id)
        if not job:
            raise HTTPException(404, "Investigation not found")
        return job

    @app.get("/api/investigations/{id}", dependencies=[Depends(authorize)])
    def job(id: str):
        return job_by_id(id)

    @app.post("/api/investigations/{id}/cancel", dependencies=[Depends(authorize)])
    def cancel(id: str):
        job = job_by_id(id)
        if id in tasks:
            tasks[id].cancel()
            job.update(status="cancelled", finished_at=now())
            db().put("investigation", job)
        return {"ok": True}

    @app.get("/api/investigations/{id}/events", dependencies=[Depends(authorize)])
    async def events(id: str, request: Request):
        job_by_id(id)

        async def stream():
            previous = None
            while not await request.is_disconnected():
                current = job_by_id(id)
                serialized = json.dumps(current, ensure_ascii=False)
                if serialized != previous:
                    yield "data: " + serialized + "\n\n"
                    previous = serialized
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
