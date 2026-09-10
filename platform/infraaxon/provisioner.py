"""Only this process receives the Docker socket. Never accepts arbitrary images or mounts."""

import os
import time
import logging
import docker
import httpx
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"last_success": None, "error": "Starting", "desired": 0, "running": 0, "missing": []}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/diagnostics"}:
            self.send_error(404)
            return
        state = dict(STATE)
        age = time.time() - state["last_success"] if state["last_success"] else None
        healthy = age is not None and age < 30 and not state["error"] and not state["missing"]
        payload = json.dumps({**state, "age_seconds": age, "healthy": healthy}).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


logging.basicConfig(level=logging.INFO)


def reconcile(client, desired):
    owner = os.getenv("INSTALLATION_ID", "infraaxon-local")
    labels = {"io.infraaxon.owner": owner}
    image_name = os.getenv("AGENT_IMAGE", "infraaxon-agent:0.1.0")
    image_id = client.images.get(image_name).id
    current = {
        c.labels.get("io.infraaxon.component"): c
        for c in client.containers.list(all=True, filters={"label": f"io.infraaxon.owner={owner}"})
    }
    wanted = {d["id"]: d for d in desired}
    for id, container in current.items():
        if (
            id not in wanted
            or container.labels.get("io.infraaxon.revision") != wanted[id]["revision"]
            or container.attrs.get("Image") != image_id
        ):
            container.remove(force=True)
            continue
        if container.status != "running":
            container.start()
        wanted.pop(id)
    for id, spec in wanted.items():
        client.containers.run(
            image_name,
            name=f"infraaxon-agent-{id}",
            detach=True,
            command=["uvicorn", "infraaxon.agent:app", "--host", "0.0.0.0", "--port", "8000"],
            labels={**labels, "io.infraaxon.component": id, "io.infraaxon.revision": spec["revision"]},
            network=os.getenv("AGENT_NETWORK", "infraaxon-agents"),
            environment={
                "COMPONENT_ID": id,
                "AGENT_TOKEN": spec["token"],
                "PLATFORM_URL": os.getenv("PLATFORM_URL", "http://platform:8000"),
                "LLM_URL": os.getenv("LLM_URL", "http://litellm:4000/v1"),
                "LLM_KEY": os.environ["LLM_KEY"],
                "LLM_MODEL": "infra-diagnostics",
            },
            read_only=True,
            tmpfs={"/tmp": "size=64m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="384m",
            restart_policy={"Name": "unless-stopped"},
        )


def main():
    client = docker.from_env()
    server = ThreadingHTTPServer(("0.0.0.0", 8000), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            r = httpx.get(
                os.getenv("PLATFORM_URL", "http://platform:8000") + "/internal/desired",
                headers={"Authorization": "Bearer " + os.environ["PLATFORM_KEY"]},
                timeout=10,
            )
            r.raise_for_status()
            desired = r.json()
            reconcile(client, desired)
            owned = client.containers.list(
                filters={"label": "io.infraaxon.owner=" + os.getenv("INSTALLATION_ID", "infraaxon-local")}
            )
            running = {c.labels.get("io.infraaxon.component") for c in owned}
            STATE.update(
                last_success=time.time(),
                error=None,
                desired=len(desired),
                running=len(running),
                missing=[c["id"] for c in desired if c["id"] not in running],
            )
        except Exception as exc:
            STATE["error"] = type(exc).__name__
            logging.warning("Agent reconciliation retry: %s", type(exc).__name__)
        time.sleep(10)


if __name__ == "__main__":
    main()
