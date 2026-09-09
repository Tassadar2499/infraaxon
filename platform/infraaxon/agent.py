import asyncio
import json
import os
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Header, HTTPException
import secrets as secretlib
from .adapters import observe, tool_schema
from .models import DiagnosisInput
from .llm import SYSTEM, completion, parse_assessment

COMPONENT_ID = os.getenv("COMPONENT_ID", "")
TOKEN = os.getenv("AGENT_TOKEN", "")
PLATFORM = os.getenv("PLATFORM_URL", "http://platform:8000")


async def spec():
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{PLATFORM}/internal/components/{COMPONENT_ID}/config", headers={"X-Agent-Token": TOKEN})
        r.raise_for_status()
        return r.json()


async def heartbeat():
    while True:
        try:
            config = await spec()
            observation = await observe(config["component"], config["secrets"])
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{PLATFORM}/internal/components/{COMPONENT_ID}/report",
                    headers={"X-Agent-Token": TOKEN},
                    json={"observation": observation},
                )
        except Exception:
            pass  # platform readiness is independent; retry on the next interval, no secrets in logs
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(heartbeat())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="InfraAxon Agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "component_id": COMPONENT_ID}


@app.post("/diagnoses")
async def diagnose(request: DiagnosisInput, authorization: str = Header(default="")):
    if not TOKEN or not secretlib.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401)
    config = await spec()
    c, credentials = config["component"], config["secrets"]
    evidence = [await observe(c, credentials, window=request.time_window_minutes, trace_id=request.trace_id)]
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "component": {k: c[k] for k in ("name", "type", "description", "settings")},
                    "question": request.question,
                    "evidence": evidence,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        for _ in range(3):
            message = await completion(messages, [tool_schema()])
            calls = message.get("tool_calls", [])
            if not calls:
                try:
                    return {
                        "component": c["name"],
                        "assessment": parse_assessment(message, evidence),
                        "evidence": evidence,
                    }
                except ValueError:
                    break
            messages.append(message)
            for call in calls[:4]:
                try:
                    if call["function"]["name"] != "inspect_component":
                        raise ValueError("Unknown tool; only inspect_component is available")
                    args = json.loads(call["function"]["arguments"])
                    # Some Ollama/LiteLLM combinations wrap a final JSON assessment as
                    # function arguments. Validate it as data; never execute it as a tool.
                    if "summary" in args and "action" not in args:
                        assessment = parse_assessment({"content": json.dumps(args)}, evidence)
                        return {"component": c["name"], "assessment": assessment, "evidence": evidence}
                    if set(args) - {"action", "query", "page_id"}:
                        raise ValueError("Unsupported tool arguments")
                    result = await observe(
                        c, credentials, **args, window=request.time_window_minutes, trace_id=request.trace_id
                    )
                    evidence.append(result)
                except (ValueError, TypeError):
                    result = {
                        "error": "Invalid tool call. Use inspect_component with action, optional query/page_id. Final answer belongs in message content, not a tool call."
                    }
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)}
                )
            if len(calls) > 4:
                raise ValueError("Tool budget exceeded")
        messages.append({"role": "user", "content": "Return your final evidence-grounded JSON assessment now."})
        final = await completion(messages)
        try:
            assessment = parse_assessment(final, evidence)
        except ValueError:
            messages.append({"role": "assistant", "content": final.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "Invalid JSON schema or evidence references. Return precisely the system JSON schema. Allowed evidence IDs: "
                    + json.dumps([e["id"] for e in evidence]),
                }
            )
            assessment = parse_assessment(await completion(messages), evidence)
        return {"component": c["name"], "assessment": assessment, "evidence": evidence}
    except Exception as exc:
        return {
            "component": c["name"],
            "assessment": None,
            "evidence": evidence,
            "error": "Model diagnosis unavailable or invalid: " + type(exc).__name__,
        }
