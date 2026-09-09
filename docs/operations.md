# Running InfraAxon

## Prerequisites

- Linux, Docker Engine and Docker Compose v2, working access to the Docker socket.
- Approximately 64 GB RAM and 100 GB free disk for the entire demonstration. The platform alone needs less; CPU inference is slower than GPU inference.
- For source development: Python 3.12/3.13 + uv, Node 22.12+ and .NET 10 SDK.

The platform, agent runtime and example have independent responsibilities. Starting InfraAxon does not create any target infrastructure or pre-register the shop.

## Start the platform

```sh
python tools/infraaxon.py init
python tools/infraaxon.py up
python tools/infraaxon.py model
```

Open http://localhost:18080 and use `PLATFORM_KEY` from the private `.env` file to sign in. Never commit this file. Initialization preserves an existing configuration; losing `ENCRYPTION_KEY` makes stored connector credentials unreadable.

`COMPOSE_BIN` can point to a standalone Compose v2 executable. The helper also detects `.tools/docker-compose` inside the project, then `docker-compose` or `docker compose`.

The first model download is about 5.2 GB. Runtime inference uses local Ollama via LiteLLM; no cloud fallback is configured. For NVIDIA hardware with working drivers and Container Toolkit:

```sh
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

Create an environment, select an adapter, enter a reachable endpoint, provide narrowly scoped credentials, then enable the agent. Configure dependencies by editing a component. Credentials may be left blank when editing to retain the saved values. Periodic observations run every 30 seconds. Stale heartbeat and target availability are separate states.

Set the optional **Адрес веб-интерфейса** field to the HTTP/HTTPS address reachable from your browser. Cards and component details show **Открыть сервис ↗**, which opens that address in a new tab. The agent still uses Endpoint for diagnostics. Clear the web address to hide the link. URLs must not contain credentials.

Shop registration uses `examples/shop/web-urls.json` for local web addresses and preserves existing values, including deliberately cleared links. Components without a published web interface have no default link.

## Independent shop example

```sh
python tools/infraaxon.py shop
python tools/bootstrap_shop.py
python tools/infraaxon.py register-shop
```

For the full collaboration suite, replace `shop` with `full`, complete the documented initial setup, and run registration with `WITH_COLLABORATION=1`.

| Interface | Local address |
|---|---|
| InfraAxon | http://localhost:18080 |
| Platform OpenAPI | http://localhost:18000/docs |
| Example shop | http://localhost:18081 |
| Shop experiments | http://localhost:18090 |
| Grafana | http://localhost:13000 |
| Kibana | http://localhost:15601 |
| Prometheus | http://localhost:19090 |
| MinIO console | http://localhost:19001 |
| OpenProject Community | http://localhost:18083 |
| Wiki.js | http://localhost:18084 |
| Mattermost | http://localhost:18085 |

OpenProject and Wiki.js are free self-hosted replacements for Jira and Confluence. The collaboration profile uses their real APIs, not mocks. See `collaboration.md` for administrator setup and fixture loading.

Open the experiments page with `SCENARIO_KEY` from `.env`. Start one fault, create synthetic orders, then investigate symptoms from InfraAxon. The controller expires faults after two minutes by default and can reset them explicitly. It is outside the platform and its state is unavailable to agents. Health endpoints never report scenario labels.

All Kafka clients use the advertised Toxiproxy listener, so failure injection also affects connections opened after metadata discovery. The Kafka-outage scenario represents a broker access-path outage, not a producer-only fault. The registered MongoDB, Redis and S3 agents probe the same proxy paths as the shop. A failed path is not proof that the underlying native server has stopped. Register a second component with the native endpoint to compare paths.

## Shutdown and recovery

```sh
python tools/infraaxon.py reset-scenarios
python tools/infraaxon.py down
```

Shutdown removes only InfraAxon-labelled agents and the two Compose projects; persistent volumes are retained. Re-running startup restores configuration and agent containers. An interrupted investigation retains its evidence. There is intentionally no automatic destructive data-reset command.

## Development and verification

```sh
uv sync --frozen
uv run pytest -q
cd web && npm ci && npm run build
```

Build/test the C# projects under `examples/shop/backend`. A local distro SDK lacking framework pruning metadata may require `-p:AllowMissingPrunePackageData=true`; the official SDK container is the reference build environment.

`python tools/smoke.py` exercises real HTTP, infrastructure observations, order idempotency and recovery. `python tools/evaluate.py` runs three real-model fault experiments and writes `artifacts/evaluation.json`. Use `--cases mongo-cascade` for the multi-agent dependency experiment, or `--repeat 3` for repeated runs. Neither treats an LLM-generated confidence score as proof of a diagnosis.

## PoC boundaries

- One operator and one agent Docker host. Endpoints may be private network addresses by design.
- Docker socket access is isolated to the provisioner. Runtime agents have no mounts, shell tool, or write tools. The shared network does not enforce per-agent network isolation.
- No agent-driven remediation, fine-tuning, vector store, production tenancy or payment processing.
- Platform SQLite and source telemetry are independent. Loss of a source appears as missing evidence, not a healthy result.
- Adapter read operations are bounded. Search scope is configured per component; do not grant broader backend permissions than needed.
- Third-party services are separate containers with their own licenses. MinIO is built from the archived pinned upstream source release; this is a local example, not a production maintenance commitment.
