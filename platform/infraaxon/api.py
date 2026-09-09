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
from .models import ComponentInput, EnvironmentInput, DiagnosisInput, TYPES
from .store import configured_store, public_component
from .adapters import observe
from .llm import synthesize

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
        credentials = (
            db().decrypt(existing["encrypted_secrets"]) if existing else {"_agent_token": secrets.token_urlsafe(32)}
        )
        credentials.update(body.secrets)
        fields = body.model_dump(exclude={"secrets"})
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
        c.update(agent_state="running", last_seen=now(), observation=data["observation"])
        db().put("component", c)
        return {"ok": True}

    def select(env_id, body):
        available = [c for c in db().all("component") if c["environment_id"] == env_id and c["enabled"]]
        if body.component_id:
            target = component(body.component_id)
            if target["environment_id"] != env_id or not target["enabled"]:
                raise HTTPException(422, "Target must be enabled in this environment")
            selected = [target]
            ids = set(target["dependencies"])
            selected += [c for c in available if c["id"] in ids or target["id"] in c["dependencies"]]
        else:
            words = set(body.question.lower().split())
            selected = sorted(
                available,
                key=lambda c: len(words & set((c["name"] + " " + c["type"] + " " + c["description"]).lower().split())),
                reverse=True,
            )
        return selected[:4]

    async def run_job(id, chosen, body, direct):
        def update(**fields):
            job = db().get("investigation", id)
            job.update(fields)
            db().put("investigation", job)
            return job

        results = []
        try:
            async with inference_lock:
                update(status="running", started_at=now())
                async with asyncio.timeout(600):
                    for c in chosen:
                        update(progress=f"Диагностика: {c['name']}")
                        try:
                            token = db().decrypt(c["encrypted_secrets"])["_agent_token"]
                            async with httpx.AsyncClient(timeout=httpx.Timeout(570, connect=5)) as client:
                                r = await client.post(
                                    f"http://infraaxon-agent-{c['id']}:8000/diagnoses",
                                    headers={"Authorization": "Bearer " + token},
                                    json=body.model_dump(),
                                )
                                r.raise_for_status()
                                results.append(r.json())
                        except Exception as exc:
                            results.append(
                                {
                                    "component": c["name"],
                                    "error": "Agent unavailable: " + type(exc).__name__,
                                    "evidence": [],
                                }
                            )
                        update(results=results)
                    evidence = [e for r in results for e in r.get("evidence", [])]
                    if direct:
                        assessment = results[0].get("assessment")
                    elif evidence:
                        update(progress="Сопоставление свидетельств")
                        names = {c["id"]: c["name"] for c in db().all("component")}
                        topology = [
                            {
                                "component": c["name"],
                                "description": c["description"],
                                "depends_on": [names.get(d, d) for d in c["dependencies"]],
                            }
                            for c in chosen
                        ]
                        assessment = await synthesize(body.question, results, topology)
                    else:
                        assessment = None
                    status = "completed" if assessment and not any(r.get("error") for r in results) else "partial"
                    update(status=status, assessment=assessment, finished_at=now(), progress="Готово")
        except asyncio.CancelledError:
            update(status="cancelled", finished_at=now(), results=results)
            raise
        except Exception as exc:
            update(
                status="partial" if results else "failed", error=type(exc).__name__, results=results, finished_at=now()
            )
        finally:
            tasks.pop(id, None)

    def start(env_id, body, direct=False):
        environment(env_id)
        chosen = select(env_id, body)
        if direct:
            chosen = chosen[:1]
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
