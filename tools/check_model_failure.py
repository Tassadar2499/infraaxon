#!/usr/bin/env python3
"""Local-demo acceptance: briefly stop LiteLLM, retain evidence, always restart it."""

import json
import subprocess
import time
from pathlib import Path
from infraaxon import request


def main():
    environments = request("/environments")
    env = next(e for e in environments if e["name"] == "Shop demo")
    for existing in environments:
        if any(j["status"] in {"running", "queued"} for j in request(f"/environments/{existing['id']}/investigations")):
            raise SystemExit("Wait for existing investigations before this local fault test")
    components = request(f"/environments/{env['id']}/components")
    worker = next(c for c in components if c["name"] == "Order worker")
    proxy = next(c for c in components if c["name"] == "LiteLLM")
    try:
        subprocess.run(["docker", "stop", "--time", "10", "infraaxon-litellm-1"], check=True, stdout=subprocess.DEVNULL)
        job = request(
            f"/components/{worker['id']}/diagnoses",
            {"question": "Проверь worker и Kafka lag, сохрани доступные наблюдения."},
        )
        deadline = time.monotonic() + 90
        while job["status"] in {"running", "queued"} and time.monotonic() < deadline:
            time.sleep(1)
            job = request("/investigations/" + job["id"])
        if job["status"] in {"running", "queued"}:
            request("/investigations/" + job["id"] + "/cancel", {})
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/profile-model-unavailable.json").write_text(json.dumps(job, ensure_ascii=False, indent=2))
        assert job["status"] == "partial"
        assert len(job["results"]) == sum(c["enabled"] for c in components)
        assert all(r["evidence"] for r in job["results"])
        assert any(e["action"] == "read_page" and e["ok"] for e in job.get("context_evidence", []))
        selected = next(r for r in job["results"] if r.get("selected"))
        assert selected.get("assessment") is None and selected.get("error", "").startswith(
            "Model diagnosis unavailable or invalid"
        )
        print(
            json.dumps(
                {
                    "status": job["status"],
                    "components_with_evidence": len(job["results"]),
                    "context_observations": len(job["context_evidence"]),
                    "model_error": selected["error"],
                },
                ensure_ascii=False,
            )
        )
    finally:
        subprocess.run(["docker", "start", "infraaxon-litellm-1"], check=True, stdout=subprocess.DEVNULL)
    for _ in range(40):
        if request(f"/components/{proxy['id']}/check", {})["ok"]:
            print("LiteLLM restored")
            return
        time.sleep(1)
    raise AssertionError("LiteLLM did not recover")


if __name__ == "__main__":
    main()
