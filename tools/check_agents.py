#!/usr/bin/env python3
"""Exercise each enabled component's actual scoped agent, without model inference.
Run: docker exec -i infraaxon-platform-1 python < tools/check_agents.py
"""

import asyncio
import json
import httpx
from infraaxon.store import configured_store
from infraaxon.profiles import resolve


async def main():
    store = configured_store()
    env = next(e for e in store.all("environment") if e["name"] == "Shop demo")
    rows = []
    limit = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=35) as client:

        async def check(c):
            p = resolve(c)
            token = store.decrypt(c["encrypted_secrets"])["_agent_token"]
            checks = [{"action": "inspect"}, *p["initial_checks"]]
            unique = list({json.dumps(x, sort_keys=True): x for x in checks}.values())
            for args in unique:
                if args["action"] == "search" and c["type"] != "elasticsearch":
                    args = {**args, "query": "Kafka"}
                async with limit:
                    try:
                        r = await client.post(
                            f"http://infraaxon-agent-{c['id']}:8000/observations",
                            headers={"Authorization": "Bearer " + token},
                            json=args,
                        )
                        r.raise_for_status()
                        ev = r.json()
                        row = {
                            "component": c["name"],
                            "profile": p["id"],
                            **args,
                            "ok": ev["ok"],
                            "bytes": len(ev["data"]),
                        }
                        if not ev["ok"]:
                            row["error"] = ev["data"][:500]
                    except Exception as exc:
                        row = {"component": c["name"], **args, "ok": False, "error": type(exc).__name__}
                    rows.append(row)

        await asyncio.gather(
            *(check(c) for c in store.all("component") if c["environment_id"] == env["id"] and c["enabled"])
        )
    store.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if not all(r["ok"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
