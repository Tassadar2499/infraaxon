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


def seed_registration(dry_run=False):
    conf = configuration()
    web_urls = json.loads((ROOT / "examples/shop/web-urls.json").read_text())
    envs = request("/environments")
    env = next((e for e in envs if e["name"] == "Shop demo"), None)
    if not env and dry_run:
        env = {"id": "", "name": "Shop demo"}
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
    from shop_agents import EXTRA_COMPONENTS, PROFILES, DEPENDENCIES, QUERIES, context_links, merge_settings

    defs += EXTRA_COMPONENTS
    profiles = {p["id"]: p for p in request("/agent-profiles")}
    existing = {c["name"]: c for c in request(f"/environments/{env['id']}/components")} if env["id"] else {}
    preview = []
    for name, kind, endpoint, settings, credentials in defs:
        profile = profiles[PROFILES[name]]
        defaults = merge_settings(profile["settings"], settings)
        if name == "Prometheus":
            defaults["queries"] = QUERIES
        if name == "LiteLLM":
            credentials = {"token": conf["LLM_KEY"]}
        c = existing.get(name)
        if c:
            body = {
                k: c.get(k, [] if k == "context_sources" else "")
                for k in [
                    "name",
                    "type",
                    "endpoint",
                    "web_url",
                    "description",
                    "settings",
                    "enabled",
                    "dependencies",
                    "agent_profile",
                    "context_sources",
                ]
            }
            # Only replace the original generic demo description and default profile.
            if not body["agent_profile"] or body["agent_profile"] == kind:
                body["agent_profile"] = profile["id"]
            if not body["description"] or body["description"].startswith("Shop demonstration component."):
                body["description"] = profile["instructions"]
            body["settings"] = merge_settings(defaults, c["settings"])
            # Existing secrets are intentionally preserved by omitting them.
        else:
            body = {
                "name": name,
                "type": kind,
                "endpoint": endpoint,
                "web_url": web_urls.get(name, ""),
                "settings": defaults,
                "secrets": credentials,
                "enabled": True,
                "description": profile["instructions"],
                "agent_profile": profile["id"],
                "context_sources": [],
                "dependencies": [],
            }
        changed = [key for key in body if key != "secrets" and (not c or body[key] != c.get(key))]
        preview.append(
            {
                "component": name,
                "operation": "create" if not c else "update" if changed else "preserve",
                "profile": body["agent_profile"],
                "fields": changed,
                "dependencies": DEPENDENCIES.get(name, []),
            }
        )
        if dry_run:
            existing[name] = {**body, "id": c["id"] if c else "planned:" + name}
        elif not c:
            existing[name] = request(f"/environments/{env['id']}/components", body)
        elif changed:
            existing[name] = request(f"/components/{c['id']}", body, "PUT")
    for name, _, _, _, _ in defs:
        c = existing[name]
        body = {
            k: c[k]
            for k in ["name", "type", "endpoint", "description", "settings", "enabled", "web_url", "agent_profile"]
        }
        body["dependencies"] = list(
            dict.fromkeys(
                c.get("dependencies", []) + [existing[d]["id"] for d in DEPENDENCIES.get(name, []) if d in existing]
            )
        )
        links = list(c.get("context_sources", []))
        for link in context_links(name, existing):
            if link not in links:
                links.append(link)
        body["context_sources"] = links
        if body["dependencies"] != c.get("dependencies", []) or links != c.get("context_sources", []):
            next(row for row in preview if row["component"] == name)["links_changed"] = True
            if not dry_run:
                request(f"/components/{c['id']}", body, "PUT")
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    print("Preview only" if dry_run else "Specialized shop agents registered through the public API.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["init", "doctor", "up", "shop", "full", "model", "register-shop", "down", "reset-scenarios"]
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview registration without changes")
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
        seed_registration(dry_run=args.dry_run)
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
