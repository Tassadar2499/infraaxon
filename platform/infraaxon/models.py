from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
from urllib.parse import urlsplit

TYPES = {
    "http": "HTTP application",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "s3": "S3 / MinIO",
    "kafka": "Apache Kafka",
    "openproject": "OpenProject Community",
    "wikijs": "Wiki.js",
    "mattermost": "Mattermost",
    "elasticsearch": "Elasticsearch",
    "kibana": "Kibana",
    "postgresql": "PostgreSQL",
    "prometheus": "Prometheus",
    "grafana": "Grafana",
}


class EnvironmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=8000)


class ComponentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str
    endpoint: str = Field(min_length=3, max_length=2048)
    description: str = Field(default="", max_length=16000)
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = False

    @field_validator("type")
    @classmethod
    def valid_type(cls, value):
        if value not in TYPES:
            raise ValueError("Unknown adapter type")
        return value

    @field_validator("secrets")
    @classmethod
    def secret_names(cls, value):
        if any(k.startswith("_") for k in value):
            raise ValueError("Reserved secret key")
        return value

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        # Internal addresses are intentional: the platform diagnoses private infrastructure.
        if "\n" in value or "\r" in value or "@" in value:
            raise ValueError("Put credentials in Secrets, not in the endpoint")
        if "://" in value and urlsplit(value).scheme not in {
            "http",
            "https",
            "mongodb",
            "mongodb+srv",
            "redis",
            "rediss",
            "postgresql",
        }:
            raise ValueError("Unsupported endpoint scheme")
        return value


class DiagnosisInput(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    component_id: str | None = None
    time_window_minutes: int = Field(default=15, ge=1, le=1440)
    trace_id: str | None = Field(default=None, max_length=128)


class Hypothesis(BaseModel):
    cause: str
    evidence_ids: list[str]


class Assessment(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    status: Literal["ok", "inconclusive"] = "inconclusive"
