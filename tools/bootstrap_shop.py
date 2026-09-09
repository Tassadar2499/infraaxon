#!/usr/bin/env python3
"""Seed the isolated shop and a read-only S3 identity. Requires a running example."""

import json
import os
import subprocess
import time
import urllib.request
from infraaxon import configuration


def main():
    conf = configuration()
    for attempt in range(90):
        try:
            with urllib.request.urlopen("http://localhost:18101/health", timeout=3):
                break
        except Exception:
            time.sleep(2)
    else:
        raise SystemExit("Catalog did not become ready")
    req = urllib.request.Request(
        "http://localhost:18101/internal/seed",
        data=b"{}",
        headers={"X-Scenario-Key": conf["SCENARIO_KEY"], "Content-Type": "application/json"},
    )
    for attempt in range(20):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                print("Catalog seeded:", json.load(r)["count"], "products")
            break
        except Exception:
            if attempt == 19:
                raise
            time.sleep(3)
    # Credentials are passed via environment, never embedded in command text or printed.
    env = {
        **os.environ,
        "MINIO_USER": conf["MINIO_ROOT_USER"],
        "MINIO_PASSWORD": conf["MINIO_ROOT_PASSWORD"],
        "MINIO_READ_PASSWORD": conf["MINIO_READ_PASSWORD"],
    }
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                    "Resource": ["arn:aws:s3:::products"],
                },
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::products/*"]},
            ],
        }
    )
    script = (
        'mc alias set local http://minio:9000 "$MINIO_USER" "$MINIO_PASSWORD" >/dev/null && '
        'mc admin user add local infraaxon-reader "$MINIO_READ_PASSWORD" >/dev/null && '
        "printf '%s' '" + policy + "' > /tmp/read-policy.json && "
        "mc admin policy create local infraaxon-products-reader /tmp/read-policy.json >/dev/null && "
        "mc admin policy attach local infraaxon-products-reader --user infraaxon-reader >/dev/null && "
        "mc admin policy detach local readonly --user infraaxon-reader >/dev/null"
    )

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "infraaxon-agents",
            "-e",
            "MINIO_USER",
            "-e",
            "MINIO_PASSWORD",
            "-e",
            "MINIO_READ_PASSWORD",
            "--entrypoint",
            "/bin/sh",
            "minio/mc:RELEASE.2025-08-13T08-35-41Z",
            "-c",
            script,
        ],
        env=env,
        check=True,
    )
    try:
        body = json.dumps(
            {
                "data_view": {"title": "infraaxon-*", "name": "InfraAxon telemetry", "timeFieldName": "@timestamp"},
                "override": True,
            }
        ).encode()
        req = urllib.request.Request(
            "http://localhost:15601/api/data_views/data_view",
            data=body,
            headers={"kbn-xsrf": "bootstrap", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20):
            print("Kibana data view created")
    except Exception:
        print("Kibana data view not ready; rerun bootstrap after Kibana starts.")


if __name__ == "__main__":
    main()
