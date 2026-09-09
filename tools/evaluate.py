#!/usr/bin/env python3
"""Real-model experiments. Fault labels stay in this evaluator, never in prompts.

Checks protocol validity and evidence, not semantic correctness of the diagnosis.
Review the saved assessments before claiming root-cause accuracy.
"""

import argparse
import json
from pathlib import Path
import time
from infraaxon import configuration, request
from smoke import http

CASES = {
    "redis-outage": ("Redis", "Каталог работает, но появились задержки. Что показывает подключённый источник?"),
    "minio-outage": ("Images", "Товары видны, но изображения перестали загружаться. Проверь доступность хранилища."),
    "prometheus-outage": ("Prometheus", "На дашбордах пропали метрики. Какие данные доступны для диагностики?"),
    "elasticsearch-outage": ("Logs", "Поиск свежих логов перестал работать. Что удалось проверить?"),
    "kafka-outage": ("Kafka", "Новые заказы остаются в ожидании. Проверь транспорт событий."),
    "worker-paused": ("Kafka", "Заказы приняты, но долго не завершаются. Что видно по обработке событий?"),
    "mongo-latency": ("MongoDB", "Каталог стал отвечать медленно. Какие наблюдения доступны?"),
    "mongo-cascade": (
        "Order worker",
        "Обработка заказов замедлилась. Сопоставь состояние обработчика и его зависимостей.",
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", nargs="+", choices=CASES, default=["redis-outage", "minio-outage", "prometheus-outage"]
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", default="artifacts/evaluation.json")
    args = parser.parse_args()
    conf = configuration()
    env = next(e for e in request("/environments") if e["name"] == "Shop demo")
    components = {c["name"]: c for c in request(f"/environments/{env['id']}/components")}
    headers = {"Authorization": "Bearer " + conf["SCENARIO_KEY"]}
    results = []
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for case in args.cases:
            target, question = CASES[case]
            for repeat in range(max(1, args.repeat)):
                code, _ = http("http://localhost:18090/activate", {"scenario": case, "seconds": 300}, headers)
                assert code == 200
                if case in {"worker-paused", "mongo-cascade", "kafka-outage"}:
                    http("http://localhost:18090/load?count=3", {}, headers)
                component = components[target]
                started = time.monotonic()
                body = {"question": question, "component_id": component["id"]}
                path = (
                    f"/environments/{env['id']}/investigations"
                    if case == "mongo-cascade"
                    else f"/components/{component['id']}/diagnoses"
                )
                job = request(path, body)
                while job["status"] in {"queued", "running"} and time.monotonic() - started < 660:
                    time.sleep(2)
                    job = request("/investigations/" + job["id"])
                if job["status"] in {"queued", "running"}:
                    request("/investigations/" + job["id"] + "/cancel", {})
                evidence = [e for r in job["results"] for e in r.get("evidence", [])]
                assessment = job.get("assessment") or {}
                known = {e["id"] for e in evidence}
                references_valid = all(
                    h["evidence_ids"] and set(h["evidence_ids"]) <= known for h in assessment.get("hypotheses", [])
                )
                result = {
                    "scenario": case,
                    "repeat": repeat + 1,
                    "duration_seconds": round(time.monotonic() - started, 1),
                    "completed": job["status"] == "completed",
                    "has_evidence": bool(evidence),
                    "references_valid": references_valid,
                    "failed_observations": sum(not e["ok"] for e in evidence),
                    "fault_ttl_seconds": 300,
                    "semantic_accuracy": "requires human review",
                    "job": job,
                }
                results.append(result)
                output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(
                    case,
                    "status=" + job["status"],
                    "seconds=" + str(result["duration_seconds"]),
                    "evidence=" + str(len(evidence)),
                    flush=True,
                )
                http("http://localhost:18090/reset", {}, headers)
    finally:
        http("http://localhost:18090/reset", {}, headers)
    print("Saved " + str(output), flush=True)


if __name__ == "__main__":
    main()
