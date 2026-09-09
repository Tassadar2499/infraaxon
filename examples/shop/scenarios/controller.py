"""Demo-only fault controller. Ground truth is never exposed to diagnostic agents."""

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import httpx

SCENARIOS = {
    "redis-outage": "Redis недоступен",
    "mongo-latency": "Задержка доступа к MongoDB",
    "minio-outage": "MinIO недоступен",
    "kafka-outage": "Kafka недоступна",
    "worker-paused": "Потребитель не продвигается",
    "mongo-cascade": "MongoDB замедляет обработку заказов",
    "prometheus-outage": "Prometheus недоступен",
    "elasticsearch-outage": "Elasticsearch недоступен",
}
current = "startup-recovery"
expires = 0.0
lock = asyncio.Lock()


def auth(authorization):
    if not secrets.compare_digest(authorization, "Bearer " + os.environ["SCENARIO_KEY"]):
        raise HTTPException(401)


async def reset():
    global current, expires
    async with httpx.AsyncClient(timeout=10) as client:
        for name in ["redis", "mongo", "minio", "kafka", "prometheus", "elasticsearch"]:
            response = await client.post(f"http://toxiproxy:8474/proxies/{name}", json={"enabled": True})
            response.raise_for_status()
            r = await client.get(f"http://toxiproxy:8474/proxies/{name}/toxics")
            r.raise_for_status()
            for toxic in r.json():
                await client.delete(f"http://toxiproxy:8474/proxies/{name}/toxics/{toxic['name']}")
        r = await client.post(
            "http://worker:8080/internal/pause?seconds=0", headers={"X-Scenario-Key": os.environ["SCENARIO_KEY"]}
        )
        r.raise_for_status()
    current = None
    expires = 0


async def watchdog():
    while True:
        try:
            async with lock:
                if current and time.monotonic() > expires:
                    await reset()
        except Exception:
            pass
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    # Recover proxy faults left by a previous controller process.
    try:
        await reset()
    except Exception:
        pass
    task = asyncio.create_task(watchdog())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="Shop demo experiments", lifespan=lifespan)


class Activation(BaseModel):
    scenario: str
    seconds: int = Field(default=120, ge=10, le=300)


@app.get("/scenarios")
def scenarios(authorization: str = Header(default="")):
    auth(authorization)
    return {"scenarios": SCENARIOS, "active": current, "remaining_seconds": max(0, round(expires - time.monotonic()))}


@app.post("/activate")
async def activate(body: Activation, authorization: str = Header(default="")):
    global current, expires
    auth(authorization)
    if body.scenario not in SCENARIOS:
        raise HTTPException(422, "Unknown scenario")
    async with lock:
        await reset()
        async with httpx.AsyncClient(timeout=10) as client:
            if body.scenario == "worker-paused":
                r = await client.post(
                    f"http://worker:8080/internal/pause?seconds={body.seconds}",
                    headers={"X-Scenario-Key": os.environ["SCENARIO_KEY"]},
                )
            elif body.scenario.startswith("mongo-"):
                r = await client.post(
                    "http://toxiproxy:8474/proxies/mongo/toxics",
                    json={
                        "name": "latency",
                        "type": "latency",
                        "stream": "downstream",
                        "attributes": {"latency": 1500, "jitter": 100},
                    },
                )
            else:
                target = body.scenario.removesuffix("-outage")
                r = await client.post(f"http://toxiproxy:8474/proxies/{target}", json={"enabled": False})
            r.raise_for_status()
        current = body.scenario
        expires = time.monotonic() + body.seconds
    return {"active": current, "seconds": body.seconds}


@app.post("/reset")
async def reset_endpoint(authorization: str = Header(default="")):
    auth(authorization)
    async with lock:
        await reset()
    return {"ok": True}


@app.post("/load")
async def load(authorization: str = Header(default=""), count: int = 10):
    auth(authorization)
    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(max(1, min(count, 30))):
            r = await client.post(
                "http://orders:8080/api/orders",
                headers={"Idempotency-Key": secrets.token_hex(16)},
                json={
                    "items": [{"productId": "1", "quantity": 1}],
                    "customerName": "Synthetic customer",
                    "address": "Demo address",
                },
            )
            results.append({"status": r.status_code, "order_id": r.json().get("id")})
    return results


@app.get("/", response_class=HTMLResponse)
def ui():
    return """<!doctype html><html lang="ru"><meta charset="utf-8"><title>Shop experiments</title><style>body{font:16px system-ui;max-width:750px;margin:60px auto;background:#111b24;color:#dee9ed}input,select,button{font:inherit;padding:10px;margin:8px;background:#223441;color:inherit;border:1px solid #537361;border-radius:5px}pre{white-space:pre-wrap}small{color:#9babb3}</style><h1>Эксперименты магазина</h1><p>Отдельный тестовый инструмент. Агенты платформы не имеют доступа к его конфигурации.</p><input id="key" type="password" placeholder="SCENARIO_KEY"><button onclick="refresh()">Подключиться</button><div><select id="scenario"></select><button onclick="run('/activate',{scenario:document.querySelector('#scenario').value,seconds:120})">Включить на 120 с</button></div><button onclick="run('/load?count=10',{})">Создать 10 заказов</button><button onclick="run('/reset',{})">Восстановить</button><pre id="result"></pre><script>async function run(path,body){try{let r=await fetch(path,{method:'POST',headers:{'Authorization':'Bearer '+document.querySelector('#key').value,'Content-Type':'application/json'},body:JSON.stringify(body)});document.querySelector('#result').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.querySelector('#result').textContent=String(e)}}async function refresh(){let r=await fetch('/scenarios',{headers:{'Authorization':'Bearer '+document.querySelector('#key').value}});let d=await r.json();if(!r.ok){document.querySelector('#result').textContent=JSON.stringify(d);return}document.querySelector('#scenario').replaceChildren(...Object.entries(d.scenarios).map(([v,t])=>{let o=document.createElement('option');o.value=v;o.textContent=t;return o}));document.querySelector('#result').textContent=JSON.stringify(d,null,2)}</script></html>"""
