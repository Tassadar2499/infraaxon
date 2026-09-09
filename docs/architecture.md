# Architecture

InfraAxon connects to existing infrastructure. It provisions its own diagnostic agents, never the infrastructure being diagnosed.

- Platform API and coordinator: Python/FastAPI; SQLite holds independent durable configuration, investigations and evidence.
- Operator console: Vue 3, TypeScript and Vite.
- Agent runtime: one container per registered component, common image with typed adapters.
- Provisioner: reconciles only containers labelled as owned by this installation; only this service accesses the Docker socket.
- Inference: local Ollama through LiteLLM. No cloud fallback.
- Integrations: MongoDB, Redis, S3/MinIO, Kafka, HTTP applications, OpenProject Community, Wiki.js, Mattermost, Elasticsearch, Kibana, PostgreSQL, Prometheus and Grafana.
- Read-only diagnosis with evidence references; no model-driven remediation.
- The separately deployed C#/ASP.NET Core shop demonstrates catalog, cache, images, orders and event processing.

OpenProject replaces Jira; Wiki.js replaces Confluence. No Atlassian license is required.
