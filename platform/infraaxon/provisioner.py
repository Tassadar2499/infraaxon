"""Only this process receives the Docker socket. Never accepts arbitrary images or mounts."""

import os
import time
import logging
import docker
import httpx

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
    while True:
        try:
            r = httpx.get(
                os.getenv("PLATFORM_URL", "http://platform:8000") + "/internal/desired",
                headers={"Authorization": "Bearer " + os.environ["PLATFORM_KEY"]},
                timeout=10,
            )
            r.raise_for_status()
            reconcile(client, r.json())
        except Exception as exc:
            logging.warning("Agent reconciliation retry: %s", type(exc).__name__)
        time.sleep(10)


if __name__ == "__main__":
    main()
