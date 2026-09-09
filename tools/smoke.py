#!/usr/bin/env python3
"""End-to-end checks against the actual running shop and platform."""

import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid
from infraaxon import configuration, request


def http(url, body=None, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def wait_order(id, timeout=50):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        code, order = http("http://localhost:18102/api/orders/" + id)
        if code == 200 and order["status"] == "completed":
            return order
        time.sleep(1)
    raise AssertionError("Order failed to complete: " + id)


def main():
    conf = configuration()
    results = []
    code, catalog = http("http://localhost:18101/api/products")
    assert code == 200 and catalog["total"] == 20
    results.append("catalog: 20 real MongoDB products")
    with urllib.request.urlopen("http://localhost:18101/api/products/1/image", timeout=20) as response:
        assert b"<svg" in response.read()
    results.append("S3 image served through Catalog.Api")
    key = str(uuid.uuid4())
    body = {
        "items": [{"productId": "1", "quantity": 2}],
        "customerName": "PoC smoke test",
        "address": "Synthetic address",
    }
    headers = {"Idempotency-Key": key}
    code, order = http("http://localhost:18102/api/orders", body, headers)
    assert code == 201 and order["totalKopecks"] == 298000
    code, repeat = http("http://localhost:18102/api/orders", body, headers)
    assert code == 200 and repeat["id"] == order["id"]
    code, _ = http("http://localhost:18102/api/orders", {**body, "address": "Different"}, headers)
    assert code == 409
    results.append("checkout: server pricing, request idempotency and conflict detection")
    wait_order(order["id"])
    results.append("Kafka event consumed and order completed")
    scenario_headers = {"Authorization": "Bearer " + conf["SCENARIO_KEY"]}
    try:
        code, _ = http("http://localhost:18090/activate", {"scenario": "redis-outage", "seconds": 60}, scenario_headers)
        assert code == 200
        code, products = http("http://localhost:18101/api/products")
        assert code == 200 and products["total"] == 20
        results.append("Redis outage: catalog falls back to MongoDB")
        http("http://localhost:18090/reset", {}, scenario_headers)
        code, _ = http("http://localhost:18090/activate", {"scenario": "kafka-outage", "seconds": 60}, scenario_headers)
        assert code == 200
        code, pending = http("http://localhost:18102/api/orders", body, {"Idempotency-Key": str(uuid.uuid4())})
        assert code == 201
        time.sleep(3)
        _, state = http("http://localhost:18102/api/orders/" + pending["id"])
        assert state["status"] == "accepted" and not state["outboxSent"]
        http("http://localhost:18090/reset", {}, scenario_headers)
        wait_order(pending["id"])
        results.append("Kafka outage: durable outbox, then recovery without lost order")
        code, _ = http(
            "http://localhost:18090/activate", {"scenario": "worker-paused", "seconds": 60}, scenario_headers
        )
        assert code == 200
        code, pending = http("http://localhost:18102/api/orders", body, {"Idempotency-Key": str(uuid.uuid4())})
        assert code == 201
        time.sleep(4)
        _, state = http("http://localhost:18102/api/orders/" + pending["id"])
        assert state["status"] == "accepted"
        http("http://localhost:18090/reset", {}, scenario_headers)
        wait_order(pending["id"])
        results.append("Worker pause and deterministic recovery")
    finally:
        http("http://localhost:18090/reset", {}, scenario_headers)
    envs = request("/environments")
    assert envs
    results.append("Platform remains available throughout infrastructure faults")
    output = {"passed": True, "checks": results}
    path = Path("/tmp/infraaxon-smoke-results.json")
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    for result in results:
        print("PASS", result)


if __name__ == "__main__":
    main()
