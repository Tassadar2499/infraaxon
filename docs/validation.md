# Validation record

Local run on 2026-09-09, Linux, Docker Engine 29.7.2, Compose 2.39.4, Python 3.12, Node 22 and .NET 10. The local Qwen3 8B model ran on CPU through LiteLLM and Ollama.

| Check | Result |
|---|---|
| Python platform tests | 17 passed |
| C# pricing and malformed checkout tests | 8 passed |
| Operator console production build | Passed |
| Shop frontend production build | Passed |
| Three C# service Docker builds | Passed |
| Browser end-to-end tests | 2 passed: register an arbitrary HTTP target; complete shop checkout |
| Real shop integration smoke test | 8 checks passed |
| Real infrastructure adapter checks | 19 operations passed across 15 registered components and 13 adapter types |
| Independent HTTP-agent diagnosis | Completed, HTTP 200 observation referenced in the response |

Integration checks cover twenty MongoDB products, a real S3 image, server-side pricing, HTTP idempotency and conflict detection, Kafka processing, Redis fallback, durable outbox recovery after Kafka access failure, worker pause/recovery and continued platform availability.

Adapter checks use actual containers, not mocks. They include Elasticsearch log search, OpenProject work-package search, Wiki.js page search and Mattermost message search, as well as native database/cache/broker/storage and observability probes. The check script prints redacted failures and response sizes; successful transport alone does not prove the contents are operationally useful.

## Real model experiments

One run per access-path outage, temperature zero, default model configuration:

| Injected access-path fault | Duration | Observations | Outcome |
|---|---:|---:|---|
| Redis | 28 s | 1 failed observation | Completed; insufficient evidence for a definitive cause |
| S3 / MinIO | 24 s | 1 failed observation | Completed; insufficient evidence for a definitive cause |
| Prometheus | 20 s | 1 failed observation | Completed; unavailable metrics were reported |

All three produced schema-valid responses referencing existing evidence. This verifies the diagnostic pipeline, not root-cause accuracy. For example, the S3 response suggested configuration as a hypothesis even though the injected cause was a network path outage. Additional observations would be necessary to distinguish those explanations. `completed` describes a finished investigation; the assessment itself can remain `inconclusive`.

A separate dependency experiment consulted the order worker, Kafka and MongoDB and completed in 328 seconds. All three observations were collected while the injected 300-second MongoDB access delay was active; synthesis finished after fault expiry. The MongoDB observation took about nine seconds. The result was inconclusive and did not establish the injected network delay as the root cause; some hypotheses overinterpreted normal database monitoring operations. This is a limitation of diagnostic quality, despite successful orchestration. The final coordinator also passes the declared dependency graph into synthesis; no accuracy improvement from that addition is claimed here.

GitHub Actions passed the Python suite, both frontend builds, C# tests and all three service builds for the initial implementation commit: [validation run](https://github.com/Tassadar2499/infraaxon/actions/runs/34387702765).

Early development runs exposed invalid local-model tool-call formatting; the agent now validates a final assessment wrapped in tool arguments as data and never executes it. A regression test covers this behavior. Earlier failed runs remain in local history rather than being relabelled as successful.

## Reproduce

```sh
uv sync --frozen
uv run pytest -q
dotnet test examples/shop/backend/Tests/Tests.csproj -c Release -p:RestoreLockedMode=true
(cd web && npm ci && npm run build)
(cd examples/shop/frontend && npm ci && npm run build)
python3 tools/smoke.py
docker compose exec -T platform python < tools/check_adapters.py
python3 tools/evaluate.py
```

For browser checks, install the matching Chromium with `npx playwright install chromium` in `web`, then run `npm test` with the local `PLATFORM_KEY` in the process environment. These tests require the running demonstration and should not be aimed at external systems. Raw evaluation results are written to gitignored `artifacts/`; no credentials are included in published reports.

This small experiment does not establish an accuracy percentage, false-positive rate, production reliability or comparative model ranking. Multi-agent investigations can take several minutes on CPU. Pin the model digest and repeat representative cases before making such claims.

## Specialized profiles acceptance — 2026-09-10

The full local stack was upgraded in place with 25 registered profiles. Additional components in other environments, component IDs, connector secrets and investigation history were retained. A pre-upgrade SQLite snapshot is stored in the platform data volume. A repeat registration preview reported no changes.

- Python regression suite: 52 passed, including profile compatibility, legacy API updates, context scoping/deduplication, routing, cancellation, unknown Kafka offsets, Redis counter resets and read-only Wiki.js page retrieval.
- C# suite: 8 passed in the official .NET 10 SDK image; Orders image built with the new outbox diagnostics endpoint. The host distro SDK lacked framework pruning metadata.
- Console and storefront production builds passed. In-app browser verification confirmed Orders profile selection, six editable context links, successful save and no console errors.
- Actual agent endpoints: 42 mandatory operations passed across all 25 components.
- Existing shop integration smoke: 8 passed, including pricing/idempotency, Redis fallback, durable outbox recovery and worker pause/recovery.
- New observation scenarios: all eight passed — Redis/S3 access outage, MongoDB latency, Kafka outage with outbox count/age, paused worker with positive lag, missing Prometheus/Elasticsearch and idle worker with zero lag. All faults were reset and the mandatory agent checks passed again.
- Additional API checks verified OpenProject work-package activity reading and nonempty exact service filters for Catalog, Orders and Worker logs. Live Elasticsearch uses dynamic text+keyword fields while historical demo events use keyword fields; the adapter now handles both mappings.

The first real-model Worker run (113 seconds) produced valid JSON/evidence references but incorrectly inferred no orders in the entire window from an empty log search and speculated about MongoDB latency. This led to the Elasticsearch mapping fix and Worker profile v2. A subsequent Worker v2 assessment no longer made that delay claim. These observations are individual examples, not a measured accuracy guarantee. Full redacted run artifacts are in the ignored `artifacts/` directory.

The six-specialist environment run obtained valid assessments from Worker, Kafka, MongoDB, Logs, Prometheus and Mattermost. Its first final-synthesis attempt hit the 180-second model HTTP timeout; the partial investigation and all observations were retained. The synthesis prompt was then compacted and its output budget reduced; the same saved specialist results were used for a separate replay rather than relabelling the original run.

A real local LiteLLM outage test retained observations for all 25 components plus eight context observations, including successfully read Wiki.js content. It returned `partial` with a model `ConnectError`; LiteLLM was restarted in `finally` and readiness was verified. All 25 components subsequently had fresh successful heartbeats.

The compact replay also hit the original 180-second upstream limit. Ollama logs confirmed cancellation at that boundary; both LiteLLM and the synthesis client were aligned to a 360-second synthesis budget, while specialist budgets remain 150 seconds and the enclosing investigation deadline remains 20 minutes.

With the longer upstream budget, one replay returned an empty JSON object. Final-generation requests now supply the actual Assessment JSON Schema (supported by the pinned Ollama adapter), and synthesis gets one bounded schema/reference repair attempt. Regression tests cover both the schema request and repair path.

The final replay with the schema completed in 61.8 seconds and returned a valid assessment with existing evidence references. Its semantic review still found unsupported claims: it suggested that Elasticsearch yellow status could affect metrics collection and order processing, repeated historical MongoDB concerns without current supporting measurements, and requested Kafka lag data already present in the evidence. Therefore this run passes transport/schema/reference validation but does **not** pass diagnostic-content acceptance. The local Qwen3 8B model remains an advisory source; the raw observations and this limitation are retained. The original six-specialist investigation remains partial in history, and the separate replay is recorded in `artifacts/profile-synthesis-replay.json`.
