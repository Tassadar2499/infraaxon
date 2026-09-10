"""Demo-only agent observation acceptance cases. No scenario labels reach agents.
Run inside the platform with SCENARIO_KEY supplied as an environment variable.
"""

import asyncio
import json
import os
import httpx
from infraaxon.store import configured_store


async def main():
    store = configured_store()
    env = next(e for e in store.all("environment") if e["name"] == "Shop demo")
    components = {c["name"]: c for c in store.all("component") if c["environment_id"] == env["id"]}
    rows = []
    async with httpx.AsyncClient(timeout=40) as client:

        async def scenario(path, body=None):
            response = await client.post(
                "http://scenarios:8000" + path,
                headers={"Authorization": "Bearer " + os.environ["SCENARIO_KEY"]},
                json=body or {},
            )
            response.raise_for_status()
            return response.json()

        async def read(name, **args):
            c = components[name]
            token = store.decrypt(c["encrypted_secrets"])["_agent_token"]
            r = await client.post(
                f"http://infraaxon-agent-{c['id']}:8000/observations",
                headers={"Authorization": "Bearer " + token},
                json=args,
            )
            r.raise_for_status()
            return r.json()

        async def drain():
            for _ in range(40):
                ev = await read("Kafka")
                outbox = await read("Orders", action="check", check_name="outbox")
                if (
                    ev["ok"]
                    and json.loads(ev["data"]).get("lag") == 0
                    and outbox["ok"]
                    and json.loads(outbox["data"])["outboxPending"] == 0
                ):
                    return
                await asyncio.sleep(1)
            raise AssertionError("Backlog did not drain")

        await scenario("/reset")
        await drain()
        baseline = await read("MongoDB")
        for fault, target in [
            ("redis-outage", "Redis"),
            ("minio-outage", "Images"),
            ("mongo-latency", "MongoDB"),
            ("worker-paused", "Order worker"),
            ("kafka-outage", "Kafka"),
            ("prometheus-outage", "Prometheus"),
            ("elasticsearch-outage", "Logs"),
        ]:
            captured = []
            try:
                await scenario("/activate", {"scenario": fault, "seconds": 120})
                if fault in ("worker-paused", "kafka-outage"):
                    await scenario("/load?count=3")
                    await asyncio.sleep(3)
                ev = await read(target)
                captured.append(ev)
                if fault == "mongo-latency":
                    assert ev["duration_ms"] >= 1000 and ev["duration_ms"] > baseline["duration_ms"]
                elif fault == "worker-paused":
                    lag = await read("Kafka")
                    captured.append(lag)
                    assert ev["ok"] and lag["ok"] and json.loads(lag["data"])["lag"] > 0
                elif fault == "kafka-outage":
                    outbox = await read("Orders", action="check", check_name="outbox")
                    captured.append(outbox)
                    data = json.loads(outbox["data"])
                    assert outbox["ok"] and data["outboxPending"] >= 1 and data["oldestPendingAgeSeconds"] > 0
                    assert not ev["ok"]
                else:
                    assert not ev["ok"]
                rows.append({"scenario": fault, "passed": True, "evidence": captured})
            except Exception as exc:
                rows.append({"scenario": fault, "passed": False, "error": type(exc).__name__, "evidence": captured})
            finally:
                await scenario("/reset")
            await drain()
        worker = await read("Order worker")
        lag = await read("Kafka")
        rows.append(
            {
                "scenario": "idle-worker",
                "passed": worker["ok"] and lag["ok"] and json.loads(lag["data"])["lag"] == 0,
                "evidence": [worker, lag],
            }
        )
    store.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if not all(r["passed"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
