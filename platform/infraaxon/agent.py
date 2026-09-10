import asyncio
import json
import os
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Header, HTTPException
import secrets as secretlib
from .adapters import observe, tool_schema
from .models import DiagnosisInput as DiagnosisInput, AgentDiagnosisInput, ObservationInput
from .profiles import resolve, system_prompt
from .llm import SYSTEM, completion, parse_assessment, prompt_evidence

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


def authorize(authorization):
    if not TOKEN or not secretlib.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401)


@app.post("/observations")
async def observations(request: ObservationInput, authorization: str = Header(default="")):
    authorize(authorization)
    config = await spec()
    args = request.model_dump()
    args["window"] = args.pop("time_window_minutes")
    credentials = dict(config["secrets"])
    if resolve(config["component"])["id"] == "platform":
        credentials["token"] = TOKEN
    return await observe(config["component"], credentials, **args)


@app.post("/diagnoses")
async def diagnose(request: AgentDiagnosisInput, authorization: str = Header(default="")):
    if not TOKEN or not secretlib.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401)
    config = await spec()
    c, credentials = config["component"], config["secrets"]
    profile = resolve(c)
    if profile["id"] == "platform":
        credentials = {**credentials, "token": TOKEN}
    metadata = {"agent_profile": profile["id"], "profile_version": profile["version"]}
    evidence = list(getattr(request, "initial_evidence", []))
    for check in profile["initial_checks"]:
        if not any(
            e.get("action") == check["action"] and e.get("check_name", "") == check.get("check_name", "")
            for e in evidence
        ):
            evidence.append(
                await observe(c, credentials, **check, window=request.time_window_minutes, trace_id=request.trace_id)
            )
    evidence.extend(getattr(request, "context_evidence", []))
    evidence = list({e["id"]: e for e in evidence}.values())
    messages = [
        {"role": "system", "content": system_prompt(c, SYSTEM)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "component": {"name": c["name"], "type": c["type"], "description": c["description"][:2500]},
                    "question": request.question,
                    "evidence": prompt_evidence(evidence),
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        async with asyncio.timeout(150):
            for _ in range(3):
                message = await completion(messages, [tool_schema(c)])
                calls = message.get("tool_calls", [])
                if not calls:
                    try:
                        return {
                            "component": c["name"],
                            **metadata,
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
                            return {"component": c["name"], **metadata, "assessment": assessment, "evidence": evidence}
                        if set(args) - set(tool_schema(c)["function"]["parameters"]["properties"]):
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
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(
                                prompt_evidence([result], 1800)[0] if "id" in result else result, ensure_ascii=False
                            ),
                        }
                    )
                if len(calls) > 4:
                    raise ValueError("Tool budget exceeded")
                if sum(len(m.get("content") or "") for m in messages) > 26000:
                    break
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
            return {"component": c["name"], **metadata, "assessment": assessment, "evidence": evidence}
    except Exception as exc:
        return {
            "component": c["name"],
            **metadata,
            "assessment": None,
            "evidence": evidence,
            "error": "Model diagnosis unavailable or invalid: " + type(exc).__name__,
        }
