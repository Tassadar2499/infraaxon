#!/usr/bin/env python3
"""Initializes only the local demonstration collaboration services.

Run with `uv run python tools/bootstrap_collaboration.py`. Generated secrets stay in .env.
"""

import json
import os
import secrets
import subprocess
import time
import uuid
import httpx
from infraaxon import configuration, ROOT


def save(values):
    conf = configuration()
    conf.update(values)
    path = ROOT / ".env"
    path.write_text("\n".join(k + "=" + v for k, v in conf.items()) + "\n")
    path.chmod(0o600)


def wait(url):
    for _ in range(90):
        try:
            if httpx.get(url, timeout=5).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Service not ready: " + url)


def wikijs():
    conf = configuration()
    password = conf.get("WIKIJS_ADMIN_PASSWORD") or "Aa1!" + secrets.token_urlsafe(24)
    save({"WIKIJS_ADMIN_PASSWORD": password})
    base = "http://localhost:18084"
    wait(base)
    with httpx.Client(timeout=90) as client:
        root = client.get(base)
        if "<setup " in root.text.lower():
            r = client.post(
                base + "/finalize",
                json={
                    "adminEmail": "admin@infraaxon.local",
                    "adminPassword": password,
                    "siteUrl": base,
                    "telemetry": False,
                },
            )
            r.raise_for_status()
            if r.json().get("ok") is False:
                raise RuntimeError("Wiki.js setup failed")
            time.sleep(8)

        def gql(query, variables=None, token=None):
            r = client.post(
                base + "/graphql",
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": "Bearer " + token} if token else {},
            )
            r.raise_for_status()
            value = r.json()
            if value.get("errors"):
                raise RuntimeError(str(value["errors"]))
            return value["data"]

        login = gql(
            'mutation($p:String!){authentication{login(username:"admin@infraaxon.local",password:$p,strategy:"local"){jwt responseResult{succeeded message}}}}',
            {"p": password},
        )["authentication"]["login"]
        token = login.get("jwt")
        if not token:
            raise RuntimeError("Wiki.js admin login failed")
        pages = gql("{pages{list(limit:100){id path}}}", token=token)["pages"]["list"]
        fixtures = [
            (
                "architecture",
                "Shop architecture",
                "Catalog.Api reads MongoDB and caches product lists in Redis for 30 seconds. Images are served from MinIO through Catalog.Api. Orders.Api checks catalog prices and stores an order with an embedded outbox record in MongoDB. The publisher sends orders.created events to Kafka. Orders.Worker consumes them, simulates payment and updates order status. All HTTP and Kafka requests propagate trace context.",
            ),
            (
                "runbooks/redis",
                "Redis troubleshooting",
                "If Redis access fails, Catalog.Api falls back to MongoDB. Check application cache warnings, cache hit/miss counters and the network path to Redis. A healthy direct Redis probe does not prove the application proxy path is healthy.",
            ),
            (
                "runbooks/kafka",
                "Kafka lag troubleshooting",
                "Distinguish an outbox publication backlog from consumer lag. First check broker reachability and order outboxPending. Then inspect consumer committed offsets and high watermarks, worker errors and MongoDB latency. Increasing lag can be a consequence of a slow downstream database.",
            ),
            (
                "runbooks/s3",
                "Image storage troubleshooting",
                "If products load but images fail, inspect the S3 access path, bucket permissions, object presence and Catalog.Api traces. A successful list of products does not confirm image storage health.",
            ),
        ]
        for path, title, content in fixtures:
            if any(p["path"] == path for p in pages):
                continue
            result = gql(
                'mutation($path:String!,$title:String!,$content:String!){pages{create(path:$path,title:$title,content:$content,description:"InfraAxon demonstration runbook",editor:"markdown",isPublished:true,isPrivate:false,locale:"en",tags:["infraaxon"]){responseResult{succeeded message} page{id}}}}',
                {"path": path, "title": title, "content": content},
                token,
            )["pages"]["create"]
            if not result["responseResult"]["succeeded"]:
                raise RuntimeError(result["responseResult"]["message"])
        groups = gql("{groups{list{id name}}}", token=token)["groups"]["list"]
        group = next((g for g in groups if g["name"] == "InfraAxon readers"), None)
        if not group:
            group = gql('mutation{groups{create(name:"InfraAxon readers"){group{id name}}}}', token=token)["groups"][
                "create"
            ]["group"]
        rule = {
            "id": str(uuid.uuid4()),
            "deny": False,
            "match": "START",
            "roles": ["read:pages", "read:assets"],
            "path": "",
            "locales": [],
        }
        result = gql(
            'mutation($id:Int!,$rules:[PageRuleInput]!){groups{update(id:$id,name:"InfraAxon readers",redirectOnLogin:"/",permissions:["read:pages","read:assets"],pageRules:$rules){responseResult{succeeded message}}}}',
            {"id": group["id"], "rules": [rule]},
            token,
        )
        gql("mutation{authentication{setApiState(enabled:true){responseResult{succeeded}}}}", token=token)
        if not conf.get("WIKIJS_API_TOKEN"):
            key = gql(
                'mutation($g:Int!){authentication{createApiKey(name:"InfraAxon diagnostics",expiration:"365d",fullAccess:false,group:$g){key responseResult{succeeded message}}}}',
                {"g": group["id"]},
                token,
            )["authentication"]["createApiKey"]["key"]
            if not key:
                raise RuntimeError("Wiki.js read key was not created")
            save({"WIKIJS_API_TOKEN": key})
    print("Wiki.js: runbooks and read-only API group ready")


def openproject():
    conf = configuration()
    password = conf.get("OPENPROJECT_ADMIN_PASSWORD") or "Aa1!" + secrets.token_urlsafe(24)
    save({"OPENPROJECT_ADMIN_PASSWORD": password})
    wait("http://localhost:18083/health_checks/default")
    ruby = r"""
require 'json'
admin=User.find_by!(login:'admin')
admin.password=ENV.fetch('DEMO_PASSWORD');admin.password_confirmation=ENV.fetch('DEMO_PASSWORD');admin.force_password_change=false;admin.save!
User.current=admin
project=Project.find_by(identifier:'infraaxon-demo') || Project.create!(name:'InfraAxon demonstration',identifier:'infraaxon-demo',public:false,workspace_type:'project')
project.enabled_module_names=['work_package_tracking']
project.types=Type.all if project.types.empty?
reader=User.find_by(login:'infraaxon-reader')
unless reader
  reader=User.new(login:'infraaxon-reader',firstname:'InfraAxon',lastname:'Reader',mail:'reader@infraaxon.local',status:1)
  reader.password=ENV.fetch('DEMO_PASSWORD');reader.password_confirmation=ENV.fetch('DEMO_PASSWORD');reader.save!
end
role=ProjectRole.find_or_initialize_by(name:'InfraAxon read only');role.permissions=[:view_work_packages];role.save!
member=Member.find_or_initialize_by(project:project,principal:reader);member.roles=[role];member.save!
[
 ['Redis access outage: catalog fallback','Synthetic historical incident. Catalog requests stayed available but cache warnings increased. Verify Redis network access and MongoDB fallback load.'],
 ['Kafka consumer lag caused by slow MongoDB','Synthetic historical incident. Broker was available; worker database calls slowed down and offsets stopped advancing. Lag was a downstream symptom.'],
 ['Images unavailable while catalog metadata works','Synthetic historical incident. S3 object access failed independently of product metadata. Inspect bucket permissions and the Catalog-to-MinIO path.']
].each do |subject,description|
  next if project.work_packages.exists?(subject:subject)
  WorkPackage.create!(project:project,subject:subject,description:description,author:admin,type:project.types.first,status:Status.first,priority:IssuePriority.first)
end
token=ENV['EXISTING_TOKEN'].to_s.empty? ? Token::API.create!(user:reader).plain_value : ENV['EXISTING_TOKEN']
puts 'INFRAAXON_RESULT='+{token:token,project_id:project.id}.to_json
"""
    env = {**os.environ, "DEMO_PASSWORD": password, "EXISTING_TOKEN": conf.get("OPENPROJECT_API_TOKEN", "")}
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "app",
            "-e",
            "DEMO_PASSWORD",
            "-e",
            "EXISTING_TOKEN",
            "infraaxon-shop-openproject-1",
            "bundle",
            "exec",
            "rails",
            "runner",
            "-",
        ],
        input=ruby,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode:
        raise RuntimeError(
            "OpenProject bootstrap failed: "
            + "\n".join(
                line[:350]
                for line in result.stderr.splitlines()
                if "/app/app/" in line or "stdin" in line or "runner_command.rb" in line
            )
        )
    line = next(x for x in result.stdout.splitlines() if x.startswith("INFRAAXON_RESULT="))
    values = json.loads(line.split("=", 1)[1])
    save({"OPENPROJECT_API_TOKEN": values["token"], "OPENPROJECT_PROJECT_ID": str(values["project_id"])})
    print("OpenProject: historical incidents and project-scoped reader ready")


def mattermost():
    conf = configuration()
    password = conf.get("MATTERMOST_ADMIN_PASSWORD") or "Aa1!" + secrets.token_urlsafe(24)
    save({"MATTERMOST_ADMIN_PASSWORD": password})
    wait("http://localhost:18085/api/v4/system/ping")

    def mm(*args, check=True):
        result = subprocess.run(
            ["docker", "exec", "infraaxon-shop-mattermost-1", "/mattermost/bin/mmctl", "--local", "--json", *args],
            capture_output=True,
            text=True,
        )
        if result.returncode and check:
            raise RuntimeError("Mattermost command failed: " + result.stderr[:400])
        return result

    for username, admin in [("infraaxon-admin", True), ("infraaxon-reader", False)]:
        if mm("user", "search", username, check=False).returncode:
            args = [
                "user",
                "create",
                "--email",
                username + "@infraaxon.local",
                "--username",
                username,
                "--password",
                password,
                "--email-verified",
                "--disable-welcome-email",
            ]
            if admin:
                args.append("--system-admin")
            result = mm(*args, check=False)
            if result.returncode:
                raise RuntimeError(
                    "Mattermost demo user creation failed: "
                    + (result.stderr + result.stdout).replace(password, "[REDACTED]")[:600]
                )
    mm("config", "set", "ServiceSettings.EnableUserAccessTokens", "true")
    mm("team", "create", "--name", "infraaxon", "--display-name", "InfraAxon demo", check=False)
    mm("team", "users", "add", "infraaxon", "infraaxon-admin", "infraaxon-reader", check=False)
    mm("channel", "create", "--team", "infraaxon", "--name", "incidents", "--display-name", "Incidents", check=False)
    mm("channel", "users", "add", "infraaxon:incidents", "infraaxon-admin", "infraaxon-reader", check=False)
    with httpx.Client(base_url="http://localhost:18085", timeout=15) as client:
        response = client.post("/api/v4/users/login", json={"login_id": "infraaxon-admin", "password": password})
        response.raise_for_status()
        admin_token = response.headers["Token"]
        headers = {"Authorization": "Bearer " + admin_token}
        team = client.get("/api/v4/teams/name/infraaxon", headers=headers).json()
        channel = client.get("/api/v4/teams/" + team["id"] + "/channels/name/incidents", headers=headers).json()
        posts = client.get("/api/v4/channels/" + channel["id"] + "/posts", headers=headers).json().get("posts", {})
        message = "[InfraAxon demo] Historical incident: growing Kafka lag was caused by slow MongoDB operations in the order worker. Check the dependency path before attributing the incident to the broker."
        if not any(p["message"] == message for p in posts.values()):
            client.post(
                "/api/v4/posts", headers=headers, json={"channel_id": channel["id"], "message": message}
            ).raise_for_status()
        if not conf.get("MATTERMOST_API_TOKEN"):
            result = mm("token", "generate", "infraaxon-reader", "InfraAxon diagnostics")
            value = json.loads(result.stdout)
            value = value[0] if isinstance(value, list) else value
            save({"MATTERMOST_API_TOKEN": value["token"]})
        save({"MATTERMOST_TEAM_ID": team["id"]})
    print("Mattermost: isolated demo team, channel and reader identity ready")


if __name__ == "__main__":
    wikijs()
    openproject()
    mattermost()
