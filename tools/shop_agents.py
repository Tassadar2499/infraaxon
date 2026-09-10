"""Declarative per-component specialization for the full demonstration stack."""

EXTRA_COMPONENTS = [
    ("Storefront", "http", "http://storefront:80", {}, {}),
    ("OpenTelemetry", "http", "http://otel:13133", {}, {}),
    ("Redis exporter", "http", "http://redis-exporter:9121", {}, {}),
    ("Kafka exporter", "http", "http://kafka-exporter:9308", {}, {}),
    ("Toxiproxy", "http", "http://toxiproxy:8474", {}, {}),
    ("Platform API", "http", "http://platform:8000", {}, {}),
    ("InfraAxon console", "http", "http://web:80", {}, {}),
    ("Provisioner", "http", "http://provisioner:8000", {}, {}),
    ("Ollama", "http", "http://ollama:11434", {}, {}),
    ("LiteLLM", "http", "http://litellm:4000", {}, {}),
]
PROFILES = {
    "Catalog": "shop-catalog",
    "Orders": "shop-orders",
    "Order worker": "shop-worker",
    "Storefront": "shop-storefront",
    "MongoDB": "mongodb",
    "Redis": "redis",
    "Images": "s3",
    "Kafka": "kafka",
    "Logs": "elasticsearch",
    "Kibana": "kibana",
    "Prometheus": "prometheus",
    "Grafana": "grafana",
    "PostgreSQL": "postgresql",
    "OpenProject": "openproject",
    "Wiki.js": "wikijs",
    "Mattermost": "mattermost",
    "OpenTelemetry": "otel",
    "Redis exporter": "redis-exporter",
    "Kafka exporter": "kafka-exporter",
    "Toxiproxy": "toxiproxy",
    "Platform API": "platform",
    "InfraAxon console": "console",
    "Provisioner": "provisioner",
    "Ollama": "ollama",
    "LiteLLM": "litellm",
}
DEPENDENCIES = {
    "Catalog": ["MongoDB", "Redis", "Images"],
    "Orders": ["Catalog", "MongoDB", "Kafka"],
    "Order worker": ["Kafka", "MongoDB"],
    "Storefront": ["Catalog", "Orders"],
    "Kibana": ["Logs"],
    "Grafana": ["Prometheus"],
    "OpenTelemetry": ["Logs"],
    "Prometheus": ["OpenTelemetry", "Redis exporter", "Kafka exporter", "Platform API"],
    "Redis exporter": ["Redis"],
    "Kafka exporter": ["Kafka"],
    "OpenProject": ["PostgreSQL"],
    "Wiki.js": ["PostgreSQL"],
    "Mattermost": ["PostgreSQL"],
    "InfraAxon console": ["Platform API"],
    "Platform API": ["LiteLLM"],
    "LiteLLM": ["Ollama"],
    "Provisioner": ["Platform API"],
    "MongoDB": ["Toxiproxy"],
    "Redis": ["Toxiproxy"],
    "Images": ["Toxiproxy"],
    "Kafka": ["Toxiproxy"],
}
QUERIES = {
    "up": "up",
    "redis": "redis_up",
    "redis-hits": "rate(redis_keyspace_hits_total[5m])",
    "redis-misses": "rate(redis_keyspace_misses_total[5m])",
    "kafka-lag": 'sum(kafka_consumergroup_lag{consumergroup="shop-worker"})',
    "catalog-errors": 'sum(rate(shop_dependency_errors_total{service_name="shop-catalog"}[5m]))',
    "orders-errors": 'sum(rate(shop_dependency_errors_total{service_name="shop-orders"}[5m]))',
    "worker-errors": 'sum(rate(shop_dependency_errors_total{service_name="shop-worker"}[5m]))',
    "worker-completed": 'sum(rate(shop_orders_completed_total{service_name="shop-worker"}[5m]))',
    "otel-export-failures": "sum(rate(otelcol_exporter_send_failed_log_records_total[5m]))",
}
SERVICE_NAMES = {"Catalog": "shop-catalog", "Orders": "shop-orders", "Order worker": "shop-worker"}
METRICS = {
    "Catalog": ["catalog-errors", "redis-hits", "redis-misses"],
    "Orders": ["orders-errors", "kafka-lag"],
    "Order worker": ["worker-errors", "worker-completed", "kafka-lag"],
    "Redis": ["redis", "redis-hits", "redis-misses"],
    "Kafka": ["kafka-lag"],
    "OpenTelemetry": ["otel-export-failures"],
    "Grafana": ["up"],
    "Redis exporter": ["redis"],
    "Kafka exporter": ["kafka-lag"],
}


def context_links(name, existing):
    links = []
    if name in SERVICE_NAMES and "Logs" in existing:
        links.append(
            {"component_id": existing["Logs"]["id"], "service_name": SERVICE_NAMES[name], "query": "", "check_name": ""}
        )
    if name in {"Images", "MongoDB"} and "Logs" in existing:
        links.append(
            {"component_id": existing["Logs"]["id"], "service_name": "shop-catalog", "query": "", "check_name": ""}
        )
    if "Prometheus" in existing:
        for query in METRICS.get(name, []):
            links.append(
                {"component_id": existing["Prometheus"]["id"], "service_name": "", "query": "", "check_name": query}
            )
    if name in {
        "Catalog",
        "Orders",
        "Order worker",
        "MongoDB",
        "Redis",
        "Images",
        "Kafka",
        "Logs",
        "OpenTelemetry",
        "Platform API",
        "LiteLLM",
        "Ollama",
    }:
        for source in ("OpenProject", "Wiki.js", "Mattermost"):
            if source in existing:
                links.append(
                    {"component_id": existing[source]["id"], "service_name": "", "query": "", "check_name": ""}
                )
    return links


def merge_settings(defaults, current):
    result = {**defaults, **current}
    for key, value in defaults.items():
        if isinstance(value, dict) and isinstance(current.get(key, {}), dict):
            result[key] = {**value, **current.get(key, {})}
    if "query" in current and "up" not in current.get("queries", {}) and "queries" in defaults:
        result["queries"]["up"] = current["query"]
    return result
