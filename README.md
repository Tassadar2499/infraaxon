# InfraAxon

**Give each infrastructure component an agent you can ask for evidence.**

InfraAxon is a self-hosted infrastructure diagnostics proof of concept. Register an existing database, cache, broker, object store, application or knowledge source through the console. InfraAxon creates a dedicated agent container, collects observations and coordinates investigations through a local language model.

The name combines **infrastructure** and **axon**, the part of a neuron that carries signals. Components remain independent; the platform connects their observations.

## What is implemented

- Vue 3 / TypeScript / Vite console: environments, component registry, dependency graph, agent status, investigations, evidence and export.
- Python / FastAPI platform and agents; encrypted connector credentials and durable SQLite history independent of monitored infrastructure.
- One agent container per enabled component, provisioned automatically on the local Docker host.
- Shared **LiteLLM → Ollama → Qwen3 8B** inference. Versioned profiles specialize agents through roles, named read checks and linked context, without separate model weights or fine-tuning.
- Read adapters for MongoDB, Redis, S3/MinIO, Kafka, HTTP applications, PostgreSQL, Elasticsearch, Kibana, Prometheus, Grafana, OpenProject, Wiki.js and Mattermost.
- Bounded read tools, evidence references, unavailable-source handling, streamed progress and cancellation. No automatic remediation.

The **ASP.NET Core microservice shop is an independent example** under [`examples/shop`](examples/shop). It includes Catalog.Api, Orders.Api, a Kafka worker and its own Vue storefront. OpenProject Community and Wiki.js replace Jira and Confluence.

## Start locally

Requires Linux, Docker Engine and Compose v2. The full example is intended for a development workstation with approximately 64 GB RAM and 100 GB free disk. CPU inference works; model download is about 5.2 GB.

```sh
git clone https://github.com/Tassadar2499/infraaxon.git
cd infraaxon
python3 tools/infraaxon.py init
python3 tools/infraaxon.py up
python3 tools/infraaxon.py model
```

Open **http://localhost:18080**. Sign in with `PLATFORM_KEY` from the generated private `.env` file. Add your own environment and reachable components; the platform starts without shop-specific configuration.

To add the independent demonstration:

```sh
python3 tools/infraaxon.py full
python3 tools/bootstrap_shop.py
uv sync --frozen
uv run python tools/bootstrap_collaboration.py
WITH_COLLABORATION=1 python3 tools/infraaxon.py register-shop
```

The store runs at **http://localhost:18081**. Use **http://localhost:18090** with `SCENARIO_KEY` to introduce a temporary fault, then investigate its symptoms through InfraAxon. Fault injection is a separate operator tool; its labels are never sent to agents.

## Documentation

- [Architecture and request flow](docs/architecture.md)
- [Startup, ports, shutdown and limitations](docs/operations.md)
- [Collaboration setup and credentials](docs/collaboration.md)
- [Validation results and model limitations](docs/validation.md)
- [OpenAPI snapshot](docs/openapi.json); live API at http://localhost:18000/docs

This is a working local PoC, not a production service or a measured root-cause accuracy guarantee. The coordinator collects baseline observations without inference, selects at most six specialists and consults at most eight linked context sources. See [agent profiles](docs/agent-profiles.md).

## License

Original project code is licensed under [Apache-2.0](LICENSE). Third-party services and models retain their own licenses. No paid Atlassian services or cloud LLM subscription are required by the example.
