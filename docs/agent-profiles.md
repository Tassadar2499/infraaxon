# Specialized diagnostic agents

InfraAxon runs one container per enabled component. A versioned profile supplies a role, interpretation rules, allowed operations and mandatory initial checks. All agents share LiteLLM → Ollama → Qwen3 8B. There is no fine-tuning or automatic remediation.

## Full demo inventory

`WITH_COLLABORATION=1 python tools/infraaxon.py register-shop` configures 25 components in `Shop demo`:

| Component | Profile | Primary checks |
|---|---|---|
| Catalog | shop-catalog | Health, GET products; MongoDB fallback and 30-second Redis TTL |
| Orders | shop-orders | Health, outbox count and oldest pending age |
| Order worker | shop-worker | Last processed time, linked errors and Kafka lag; idle is not failure |
| Storefront | shop-storefront | HTML, local assets, proxied catalog |
| MongoDB | mongodb | Ping, statistics, current operations |
| Redis | redis | Memory, evictions, hit/miss deltas when counters have not reset |
| Images | s3 | Bucket and configured object metadata |
| Kafka | kafka | Brokers, topic partitions, offsets and lag; unknown offsets yield null lag |
| PostgreSQL | postgresql | Connections, long transactions and blocking PIDs |
| Logs | elasticsearch | Cluster health, recent logs/traces with service and trace filters |
| Prometheus | prometheus | Scrape health/freshness, named PromQL queries |
| Grafana | grafana | Health, datasources, configured dashboard |
| Kibana | kibana | Service and Elasticsearch status |
| OpenTelemetry | otel | Collector health, internal receiver/exporter counters |
| Redis exporter | redis-exporter | Metrics and redis_up |
| Kafka exporter | kafka-exporter | Broker/topic/consumer metrics |
| Toxiproxy | toxiproxy | Version endpoint only; scenario settings are excluded |
| OpenProject | openproject | Scoped work-package search and activity history |
| Wiki.js | wikijs | Page search and published-page reading with timestamps |
| Mattermost | mattermost | Team/channel-scoped incident discussions |
| Platform API | platform | Health, SQLite reads, queue and agent heartbeat summary |
| InfraAxon console | console | HTML/assets; linked Platform API dependency |
| Provisioner | provisioner | Reconciliation age, Docker errors, desired/running/missing agents |
| Ollama | ollama | Installed and loaded models, without inference |
| LiteLLM | litellm | Proxy liveness and configured model routes, without inference |

The controller, one-shot initialization jobs and diagnostic containers do not receive recursive agents. Existing user components and other environments are preserved.

## Configuration

`GET /api/agent-profiles` returns profile IDs, versions, compatible adapter types, instructions, supported actions, initial checks and default settings. Components accept `agent_profile` and `context_sources` in the existing create/update APIs. Legacy components default to their adapter profile; updates that omit the new fields retain saved values.

A context link contains `component_id` and optional `service_name`, `query`, `check_name`. Sources must be Elasticsearch, Prometheus, OpenProject, Wiki.js or Mattermost components in the same environment. Deleting a source removes its links while retaining historical evidence. Context links are distinct from runtime `dependencies`.

The console exposes profile selection and editable source filters. Profile defaults include named HTTP `checks` and Prometheus `queries`. The model selects names, never arbitrary addresses, SQL or PromQL. Operators can configure relative GET paths; an optional named `check_ports` entry selects another port on the same host, used for collector metrics. Database operations are fixed read queries. Platform diagnostics accept only the token of an enabled platform-profile agent.

Registration adds missing defaults and links, upgrades original generic demo descriptions/profiles and retains non-default customizations. It does not overwrite existing connector secrets. Use `--dry-run` for a preview with no writes. Applying the same registration again produces no changes. Disabled collaboration does not delete existing components.

## Investigation flow and limits

1. Collect baseline observations from all enabled components, with concurrency four and a 25-second limit per observation. Save each result immediately.
2. Select the explicit target first, then direct dependencies, linked sources and reverse dependencies. Without a target, rank Russian/English symptom keywords and failed observations; ties use registry order. Limit: six specialists, or one for a direct diagnosis.
3. Collect mandatory checks and context from at most eight sources. Identical source requests are shared; filters distinguish different service searches. For Wiki.js, the first matching published page is also read, so linked specialists receive the runbook content and timestamp. Only evidence is passed between agents, never connector credentials.
4. Obtain specialist assessments sequentially. Each specialist has up to three tool rounds, four calls per round and a 150-second model budget. Each synthesis request has a 360-second budget, with a compact prompt, 1,100-token output budget and at most one schema repair; the complete investigation still has a 20-minute deadline.
5. Synthesize specialist assessments using the Assessment JSON Schema and one bounded repair attempt, against unique evidence IDs and the dependency graph. Save profiles/versions, selection reasons, skipped sources and incomplete-data reasons. JSON and Markdown exports include profile and routing information.

Heartbeat runs approximately every 30 seconds without model calls. Model failure, cancellation and timeout preserve collected observations. Wiki.js 2.5 published pages are read through their ordinary HTML route after resolving reader-visible metadata (bounded to 1,000 pages); this avoids the management permission required internally by its GraphQL single-page resolver. No edit permission is granted. Excerpts sent to the model are bounded and marked as truncated; complete bounded adapter evidence remains in history. A valid evidence reference proves provenance, not causal correctness. Historical documents and discussions are context, not proof of a current failure.

## Validation

```sh
uv run pytest -q
npm --prefix web run build
npm --prefix examples/shop/frontend run build
docker exec -i infraaxon-platform-1 python < tools/check_agents.py
python tools/smoke.py
python tools/check_model_failure.py
```

`tools/check_model_failure.py` requires an idle local platform, temporarily stops only the local LiteLLM container, verifies partial results with retained observations and restarts it in `finally`.

`tools/check_profile_scenarios.py` runs inside the platform with `SCENARIO_KEY` supplied via the process environment. It exercises Redis/S3 access failure, MongoDB latency, Kafka/outbox failure, worker lag/idle and missing telemetry through real agent observation endpoints. Every injected scenario uses a TTL and is reset in `finally`. Never run these cases against external infrastructure.

Real-model results need semantic review in addition to JSON/evidence validation. See the dated validation record for actual runs and remaining limitations.
