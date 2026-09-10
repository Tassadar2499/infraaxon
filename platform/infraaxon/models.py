from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
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


class ContextSource(BaseModel):
    model_config = {"extra": "forbid"}
    component_id: str
    service_name: str = Field(default="", max_length=120)
    query: str = Field(default="", max_length=500)
    check_name: str = Field(default="", max_length=120)


class AgentProfile(BaseModel):
    id: str
    version: int
    type: str
    name: str
    instructions: str
    keywords: list[str]
    actions: list[str]
    initial_checks: list[dict[str, str]]
    settings: dict[str, Any]


class ComponentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str
    endpoint: str = Field(min_length=3, max_length=2048)
    web_url: str = Field(default="", max_length=2048)
    description: str = Field(default="", max_length=16000)
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = False
    agent_profile: str = ""
    context_sources: list[ContextSource] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def compatible_profile(self):
        from .profiles import resolve, settings_for

        resolve(self.model_dump())
        for key in ("checks", "queries", "check_ports"):
            if not isinstance(self.settings.get(key, {}), dict):
                raise ValueError("Named checks and queries must be objects")
        settings = settings_for(self.model_dump())
        for port in settings.get("check_ports", {}).values():
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError("Invalid check port")
        for name, path in settings.get("checks", {}).items():
            if (
                not isinstance(name, str)
                or not isinstance(path, str)
                or not path.startswith("/")
                or path.startswith("//")
                or "\\" in path
                or any(ord(c) < 32 for c in path)
            ):
                raise ValueError("Checks must be named local HTTP paths")
        if any(not isinstance(v, str) or len(v) > 4000 for v in settings.get("queries", {}).values()):
            raise ValueError("Invalid named metrics query")
        return self

    @field_validator("web_url")
    @classmethod
    def valid_web_url(cls, value):
        if not value:
            return value
        try:
            url = urlsplit(value)
            port = url.port
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.username is None
                and url.password is None
                and (port is None or port > 0)
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                and "\\" not in value
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an absolute HTTP/HTTPS web URL without credentials")
        return value

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


class ObservationInput(BaseModel):
    model_config = {"extra": "forbid"}
    action: str = "inspect"
    query: str = Field(default="", max_length=500)
    page_id: int | None = Field(default=None, ge=1)
    check_name: str = Field(default="", max_length=120)
    service_name: str = Field(default="", max_length=120)
    time_window_minutes: int = Field(default=15, ge=1, le=1440)
    trace_id: str | None = Field(default=None, max_length=128)


class AgentDiagnosisInput(DiagnosisInput):
    initial_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    context_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


class Hypothesis(BaseModel):
    cause: str
    evidence_ids: list[str]


class Assessment(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    status: Literal["ok", "inconclusive"] = "inconclusive"
