#!/usr/bin/env python3
"""Small, dependency-free local operator CLI. Never prints credentials."""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def configuration():
    path = ROOT / ".env"
    if not path.exists():
        raise SystemExit("Run: python tools/infraaxon.py init")
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def compose(*args, shop=False, full=False):
    executable = (
        os.getenv("COMPOSE_BIN")
        or (str(ROOT / ".tools/docker-compose") if (ROOT / ".tools/docker-compose").exists() else None)
        or shutil.which("docker-compose")
    )
    cmd = [executable] if executable else ["docker", "compose"]
    cmd += [
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / ("examples/shop/compose.yaml" if shop else "compose.yaml")),
    ]
    if full:
        cmd += ["--profile", "collaboration"]
    subprocess.run(cmd + list(args), cwd=ROOT, check=True)


def request(path, body=None, method=None):
    conf = configuration()
    req = urllib.request.Request(
        "http://127.0.0.1:18000/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + conf["PLATFORM_KEY"], "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def seed_registration():
    conf = configuration()
    web_urls = json.loads((ROOT / "examples/shop/web-urls.json").read_text())
    envs = request("/environments")
    env = next((e for e in envs if e["name"] == "Shop demo"), None)
    if not env:
        env = request(
            "/environments",
            {
                "name": "Shop demo",
                "description": "Independent ASP.NET Core example connected through the public InfraAxon API.",
            },
        )
    defs = [
        ("Catalog", "http", "http://catalog:8080", {}, {}),
        ("Orders", "http", "http://orders:8080", {}, {}),
        ("Order worker", "http", "http://worker:8080", {}, {}),
        ("MongoDB", "mongodb", "mongodb://toxiproxy:18666", {}, {}),
        ("Redis", "redis", "redis://toxiproxy:16379", {}, {}),
        (
            "Images",
            "s3",
            "http://toxiproxy:19000",
            {"bucket": "products", "object_key": "1.svg"},
            {"access_key": "infraaxon-reader", "secret_key": conf["MINIO_READ_PASSWORD"]},
        ),
        ("Kafka", "kafka", "kafka:9092", {"topic": "orders.created", "group_id": "shop-worker"}, {}),
        ("Logs", "elasticsearch", "http://toxiproxy:19200", {"index": "infraaxon-*"}, {}),
        ("Kibana", "kibana", "http://kibana:5601", {}, {}),
        ("Prometheus", "prometheus", "http://toxiproxy:19091", {"query": "up"}, {}),
        ("Grafana", "grafana", "http://grafana:3000", {}, {}),
        (
            "PostgreSQL",
            "postgresql",
            "postgresql://postgres:5432/postgres",
            {},
            {"username": "infraaxon_reader", "password": conf["POSTGRES_READ_PASSWORD"]},
        ),
    ]
    if os.getenv("WITH_COLLABORATION") == "1":
        defs += [
            (
                "OpenProject",
                "openproject",
                "http://openproject:80",
                {"project_id": conf.get("OPENPROJECT_PROJECT_ID", "")},
                {"token": conf.get("OPENPROJECT_API_TOKEN", "")},
            ),
            ("Wiki.js", "wikijs", "http://wikijs:3000", {}, {"token": conf.get("WIKIJS_API_TOKEN", "")}),
            (
                "Mattermost",
                "mattermost",
                "http://mattermost:8065",
                {"team_id": conf.get("MATTERMOST_TEAM_ID", ""), "channel_name": "incidents"},
                {"token": conf.get("MATTERMOST_API_TOKEN", "")},
            ),
        ]
    existing = {c["name"]: c for c in request(f"/environments/{env['id']}/components")}
    for name, kind, endpoint, settings, credentials in defs:
        if name not in existing:
            existing[name] = request(
                f"/environments/{env['id']}/components",
                {
                    "name": name,
                    "type": kind,
                    "endpoint": endpoint,
                    "web_url": web_urls.get(name, ""),
                    "settings": settings,
                    "secrets": credentials,
                    "enabled": True,
                    "description": "Shop demonstration component. Investigate live observations, not predefined incident labels.",
                },
            )
        else:
            c = existing[name]
            body = {k: c[k] for k in ["name", "type", "endpoint", "description", "settings", "enabled", "dependencies"]}
            body.update(endpoint=endpoint, settings=settings, secrets=credentials)
            body["web_url"] = c.get("web_url", web_urls.get(name, ""))
            existing[name] = request(f"/components/{c['id']}", body, "PUT")
    links = {
        "Catalog": ["MongoDB", "Redis", "Images"],
        "Orders": ["Catalog", "MongoDB", "Kafka"],
        "Order worker": ["Kafka", "MongoDB"],
        "Kibana": ["Logs"],
        "Grafana": ["Prometheus"],
        "OpenProject": ["PostgreSQL"],
        "Wiki.js": ["PostgreSQL"],
        "Mattermost": ["PostgreSQL"],
    }
    for name, deps in links.items():
        if name in existing:
            c = existing[name]
            body = {k: c[k] for k in ["name", "type", "endpoint", "description", "settings", "enabled"]}
            body["dependencies"] = [existing[d]["id"] for d in deps]
            request(f"/components/{c['id']}", body, "PUT")
    print("Shop registered through the public API. Console: http://localhost:18080")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["init", "doctor", "up", "shop", "full", "model", "register-shop", "down", "reset-scenarios"]
    )
    args = parser.parse_args()
    if args.command == "init":
        path = ROOT / ".env"
        if path.exists():
            print("Existing .env preserved.")
            return
        conf = {
            "PLATFORM_KEY": secrets.token_urlsafe(32),
            "ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "LLM_KEY": "sk-" + secrets.token_urlsafe(32),
            "MINIO_ROOT_USER": "infraaxon",
            "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(32),
            "MINIO_READ_PASSWORD": secrets.token_urlsafe(24),
            "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
            "POSTGRES_READ_PASSWORD": secrets.token_urlsafe(24),
            "OPENPROJECT_SECRET_KEY_BASE": secrets.token_hex(64),
            "SCENARIO_KEY": secrets.token_urlsafe(32),
        }
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write("\n".join(f"{k}={v}" for k, v in conf.items()) + "\n")
        print("Created private .env. Operator login uses PLATFORM_KEY from that file.")
    elif args.command == "doctor":
        for command in (
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            ["dotnet", "--version"],
            ["node", "--version"],
        ):
            subprocess.run(command, check=False)
        compose("version")
    elif args.command == "up":
        compose("up", "-d", "--build")
    elif args.command in {"shop", "full"}:
        compose("up", "-d", "--build", shop=True, full=args.command == "full")
    elif args.command == "model":
        compose("exec", "-T", "ollama", "ollama", "pull", "qwen3:8b")
    elif args.command == "register-shop":
        seed_registration()
    elif args.command == "down":
        # Provisioned agents live outside Compose; stop only this installation's containers.
        ids = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "label=io.infraaxon.owner=infraaxon-local"], text=True
        ).split()
        compose("stop", "provisioner")
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], check=True)
        compose("down", shop=True, full=True)
        compose("down")
    elif args.command == "reset-scenarios":
        conf = configuration()
        req = urllib.request.Request(
            "http://localhost:18090/reset",
            data=b"{}",
            headers={"Authorization": "Bearer " + conf["SCENARIO_KEY"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            print(response.status)


if __name__ == "__main__":
    main()
