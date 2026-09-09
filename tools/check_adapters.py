#!/usr/bin/env python3
"""Read-only smoke check inside the platform container: python < tools/check_adapters.py.

Exercises real adapters and collaboration search without model inference.
Prints only redacted observation data on failure and aggregate result sizes on success.
"""

import asyncio
import json
from infraaxon.store import configured_store
from infraaxon.adapters import observe


async def main():
    store = configured_store()
    env = next(e for e in store.all("environment") if e["name"] == "Shop demo")
    results = []
    for component in store.all("component"):
        if component["environment_id"] != env["id"]:
            continue
        credentials = {k: v for k, v in store.decrypt(component["encrypted_secrets"]).items() if not k.startswith("_")}
        actions = ["inspect"]
        if component["type"] in {"openproject", "wikijs", "mattermost", "elasticsearch"}:
            actions.append("search")
        for action in actions:
            result = await observe(
                component, credentials, action=action, query="" if component["type"] == "elasticsearch" else "Kafka"
            )
            record = {
                "component": component["name"],
                "action": action,
                "ok": result["ok"],
                "bytes": len(result["data"]),
            }
            if not result["ok"]:
                record["error"] = result["data"][:600]
            results.append(record)
    store.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not all(r["ok"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
