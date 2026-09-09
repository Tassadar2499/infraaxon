"""Executed inside the local platform container by populate_demo.py."""

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import httpx
import psycopg
import redis
from pymongo import MongoClient


def main():
    mongo = MongoClient("mongodb://mongo:27017", serverSelectionTimeoutMS=10000)
    products = list(mongo.catalog.products.find())
    orders = list(mongo.orders.orders.find({"IdempotencyKey": {"$regex": "^infraaxon-demo-v1-"}}))
    customers = [
        {"id": i, "name": f"Демо-покупатель {i:02d}", "email": f"customer-{i:02d}@example.invalid",
         "city": ["Демо-Север", "Демо-Центр", "Демо-Юг"][i % 3]}
        for i in range(1, 13)
    ]
    cache = redis.Redis(host="redis", decode_responses=True)
    for customer in customers:
        cache.hset(f"infraaxon:demo:customer:{customer['id']:02d}", mapping=customer)
    cache.hset("infraaxon:demo:inventory", mapping={p["_id"]: 25 + int(p["_id"]) * 3 for p in products})
    cache.zadd("infraaxon:demo:popular-products", {p["Name"]: 100 - int(p["_id"]) * 3 for p in products})
    cache.set("infraaxon:demo:description", "Синтетические примеры. Ключ catalog:products заполняется приложением.")

    with psycopg.connect(host="postgres", user="postgres", password=os.environ["POSTGRES_PASSWORD"], dbname="postgres") as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS infraaxon_demo")
        conn.execute("CREATE TABLE IF NOT EXISTS infraaxon_demo.customers (id int PRIMARY KEY, name text, email text, city text)")
        conn.execute("CREATE TABLE IF NOT EXISTS infraaxon_demo.inventory (product_id text PRIMARY KEY, product_name text, warehouse text, quantity int, price_kopecks bigint)")
        conn.execute("CREATE TABLE IF NOT EXISTS infraaxon_demo.orders (id text PRIMARY KEY, customer_name text, status text, total_kopecks bigint, created_at timestamptz)")
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO infraaxon_demo.customers VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,email=EXCLUDED.email,city=EXCLUDED.city",
                               [(c["id"], c["name"], c["email"], c["city"]) for c in customers])
            cursor.executemany("INSERT INTO infraaxon_demo.inventory VALUES (%s,%s,%s,%s,%s) ON CONFLICT(product_id) DO UPDATE SET product_name=EXCLUDED.product_name,warehouse=EXCLUDED.warehouse,quantity=EXCLUDED.quantity,price_kopecks=EXCLUDED.price_kopecks",
                               [(p["_id"], p["Name"], ["Север", "Центр", "Юг"][int(p["_id"]) % 3], 25 + int(p["_id"]) * 3, p["PriceKopecks"]) for p in products])
            cursor.executemany("INSERT INTO infraaxon_demo.orders VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status,total_kopecks=EXCLUDED.total_kopecks",
                               [(o["_id"], o["CustomerName"], o["Status"], o["TotalKopecks"], o["CreatedAt"].replace(tzinfo=timezone.utc)) for o in orders])
        conn.execute("GRANT USAGE ON SCHEMA infraaxon_demo TO infraaxon_reader")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA infraaxon_demo TO infraaxon_reader")

    s3 = boto3.client("s3", endpoint_url="http://minio:9000", aws_access_key_id=os.environ["MINIO_ROOT_USER"],
                      aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"], region_name="us-east-1")
    bucket = "infraaxon-demo"
    if bucket not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=bucket)
    inventory_csv = io.StringIO()
    writer = csv.writer(inventory_csv)
    writer.writerow(["product_id", "name", "price_kopecks", "quantity"])
    writer.writerows((p["_id"], p["Name"], p["PriceKopecks"], 25 + int(p["_id"]) * 3) for p in products)
    files = {
        "README.md": "# InfraAxon demo\n\nСинтетические данные для изучения инфраструктуры.\n\n- reports/: реальные результаты демонстрационных заказов\n- exports/: тестовые покупатели и остатки\n- runbooks/: инструкции\n",
        "exports/inventory.csv": inventory_csv.getvalue(),
        "exports/customers.json": json.dumps(customers, ensure_ascii=False, indent=2),
        "reports/orders.json": json.dumps(orders, ensure_ascii=False, indent=2, default=str),
        "reports/summary.json": json.dumps({"demo": True, "products": len(products), "orders": len(orders), "completed": sum(o["Status"] == "completed" for o in orders), "revenue_kopecks": sum(o["TotalKopecks"] for o in orders)}, indent=2),
        "runbooks/catalog.md": "# Каталог\n\nMongoDB хранит товары. Redis кеширует список на 30 секунд. MinIO хранит SVG. Проверка: открыть магазин, товар и изображение. При сбое кеша каталог читает MongoDB.\n",
        "runbooks/orders.md": "# Заказы\n\nOrders.Api сохраняет заказ и outbox в MongoDB. Kafka переносит orders.created. Worker завершает заказ и подтверждает offset. Сравнивайте outboxPending, consumer lag и статус заказа.\n",
        "runbooks/observability.md": "# Наблюдаемость\n\nGrafana показывает метрики Prometheus, Kibana — infraaxon-logs и infraaxon-traces. Синтетические исторические примеры выделены в infraaxon-demo-events.\n",
    }
    for key, body in files.items():
        content_type = "application/json" if key.endswith(".json") else "text/csv; charset=utf-8" if key.endswith(".csv") else "text/markdown; charset=utf-8"
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType=content_type, Metadata={"dataset": "infraaxon-demo-v1"})

    now = datetime.now(timezone.utc)
    events = []
    examples = [
        ("shop-catalog", "INFO", "Демонстрационный просмотр каталога: Redis cache hit", "cache-hit"),
        ("shop-orders", "INFO", "Демонстрационный заказ принят и записан в outbox", "order-accepted"),
        ("shop-worker", "INFO", "Демонстрационный заказ успешно обработан", "order-completed"),
        ("shop-catalog", "WARN", "Исторический учебный пример: Redis недоступен, чтение из MongoDB", "historical-cache-fallback"),
        ("shop-worker", "WARN", "Исторический учебный пример: задержка MongoDB увеличивает consumer lag", "historical-consumer-lag"),
        ("shop-catalog", "ERROR", "Исторический учебный пример: отказ чтения объекта S3; текущий сбой не имитируется", "historical-image-error"),
    ]
    for i in range(180):
        service, level, message, action = examples[i % len(examples)]
        events.extend([{"index": {"_index": "infraaxon-demo-events", "_id": f"demo-v1-{i:03d}"}},
                       {"@timestamp": (now - timedelta(seconds=(179 - i) * 5)).isoformat(), "service": {"name": service},
                        "log": {"level": level}, "message": "[SYNTHETIC DEMO] " + message,
                        "event": {"dataset": "infraaxon.demo", "action": action}, "labels": {"synthetic": "true", "dataset": "infraaxon-demo-v1"}}])
    with httpx.Client(timeout=30) as client:
        mapping = {"mappings": {"properties": {"@timestamp": {"type": "date"}, "service": {"properties": {"name": {"type": "keyword"}}}, "log": {"properties": {"level": {"type": "keyword"}}}}}}
        if client.head("http://elasticsearch:9200/infraaxon-demo-events").status_code == 404:
            client.put("http://elasticsearch:9200/infraaxon-demo-events", json=mapping).raise_for_status()
        result = client.post("http://elasticsearch:9200/_bulk?refresh=true", content="\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", headers={"Content-Type": "application/x-ndjson"})
        result.raise_for_status()
        if result.json()["errors"]:
            raise RuntimeError("Some demonstration events failed to index")
    print(json.dumps({"postgres": {"customers": len(customers), "inventory": len(products), "orders": len(orders)},
                      "redis_demo_keys": len(list(cache.scan_iter("infraaxon:demo:*"))), "s3_demo_files": len(files), "synthetic_log_events": 180}))


if __name__ == "__main__":
    main()
