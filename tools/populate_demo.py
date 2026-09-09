#!/usr/bin/env python3
"""Populate the running local demo without removing existing data.

Run: .venv/bin/python tools/populate_demo.py
Orders and fixtures have stable identities, so reruns preserve existing records.
"""

import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from infraaxon import ROOT, configuration, request


PAGES = [
    ("demo/overview", "Демонстрационный стенд InfraAxon", """# Что посмотреть

Все покупатели и операции в этом наборе вымышлены. Платежи моделируются внутри Worker; внешних списаний нет.

## Маршрут знакомства
1. [Магазин](http://localhost:18081): 20 товаров в трёх категориях, корзина и оформление заказа.
2. [Grafana](http://localhost:13000/d/infraaxon): завершённые заказы, cache hits/misses и Kafka lag.
3. [Kibana](http://localhost:15601/app/discover): реальные журналы и трассировки, а также отдельный синтетический учебный журнал.
4. [OpenProject](http://localhost:18083/projects/infraaxon-demo/work_packages): задачи развития и исторические инциденты.
5. [Mattermost](http://localhost:18085/infraaxon/channels/incidents): учебные обсуждения.
6. [MinIO](http://localhost:19001): изображения в products; выгрузки, отчёты и инструкции в infraaxon-demo.

## Хранилища
MongoDB: catalog.products и orders.orders. PostgreSQL: схема infraaxon_demo с customers, inventory и orders. Redis: кеш приложения и учебные ключи infraaxon:demo:*. Kafka: реальные события orders.created.

Синтетические исторические события помечены SYNTHETIC DEMO и не означают текущий сбой.
"""),
    ("demo/order-lifecycle", "Жизненный цикл заказа", """# От корзины до completed

1. Orders.Api получает товары и актуальные цены из Catalog.Api.
2. Заказ и признак OutboxSent=false записываются вместе в MongoDB.
3. OutboxPublisher отправляет orders.created в Kafka и помечает публикацию.
4. Worker получает событие, переводит заказ в processing, моделирует обработку и записывает completed.
5. Offset подтверждается после успешного обновления заказа.

## Что проверять
GET /health у Orders показывает outboxPending. Сравните его с Kafka consumer lag и журналами shop-worker. Один лишь рост lag не доказывает отказ брокера.

## Повторные запросы
Заголовок Idempotency-Key предотвращает дублирование заказа. Повтор того же запроса возвращает существующий заказ; другое тело с тем же ключом даёт 409.
"""),
    ("demo/cache", "Кеш каталога и Redis", """# Кеширование каталога

Ключ catalog:products хранит список товаров 30 секунд. На промахе приложение читает MongoDB и заполняет кеш; при ошибке Redis продолжает читать MongoDB.

## Практика
Несколько раз откройте магазин и смените категорию. В Grafana сравните скорость cache hits и misses. После истечения TTL появится новый miss.

## Учебные ключи
infraaxon:demo:customer:* — hash с вымышленным покупателем; infraaxon:demo:inventory — остатки; infraaxon:demo:popular-products — sorted set популярности. Эти ключи отделены от рабочего кеша.
"""),
    ("demo/storage", "Хранилища и тестовые выгрузки", """# Данные магазина

MongoDB содержит товары и заказы, созданные через реальный API. В PostgreSQL схема infraaxon_demo содержит вымышленных покупателей, остатки и снимок демонстрационных заказов.

## MinIO
- products: 20 SVG-изображений товаров.
- infraaxon-demo/exports: inventory.csv и customers.json.
- infraaxon-demo/reports: заказы и итоговая сумма в копейках.
- infraaxon-demo/runbooks: инструкции по каталогу, заказам и наблюдаемости.

## SQL-примеры
```sql
SELECT status, count(*), sum(total_kopecks) / 100.0 AS amount_rub
FROM infraaxon_demo.orders GROUP BY status;
SELECT product_name, warehouse, quantity FROM infraaxon_demo.inventory ORDER BY quantity;
```

Пароли остаются в локальном .env и не включаются в выгрузки.
"""),
    ("demo/observability", "Метрики, журналы и трассировки", """# Где искать свидетельства

Prometheus собирает platform, shop, redis и kafka каждые 10 секунд. Grafana показывает scrape targets, investigations, consumer lag, Redis hits/misses и completed orders.

## PromQL
```promql
sum(shop_orders_completed_total)
sum(kafka_consumergroup_lag) by (consumergroup)
rate(redis_keyspace_hits_total[1m])
```

## Kibana
infraaxon-logs и infraaxon-traces содержат реальную телеметрию стенда. infraaxon-demo-events содержит 180 явно помеченных синтетических событий за последние 15 минут — для освоения поиска по service.name и log.level. Не используйте их как доказательство текущего инцидента.

Для реального заказа сопоставляйте trace.id, журнал приёма Orders и журнал завершения Worker.
"""),
    ("demo/incidents", "Три учебных разбора инцидентов", """# Исторические синтетические примеры

## Redis недоступен
Симптом: каталог отвечает, но растёт нагрузка на MongoDB. Свидетельства: предупреждения cache read failed и снижение cache hits. Проверять путь приложения через Toxiproxy, а не только сам Redis.

## Kafka lag из-за базы данных
Симптом: заказы долго остаются processing. Брокер доступен; Worker медленно обновляет MongoDB. Сравнить offsets, задержку базы и outboxPending до выводов о Kafka.

## Изображения недоступны
Метаданные товаров загружаются, изображения — нет. Проверить S3-путь, bucket products, объект и разрешения. Наличие товаров в MongoDB ничего не говорит о доступности MinIO.

В этом наполнении реальные неисправности не включаются; примеры служат учебной базой знаний.
"""),
]


def traffic():
    orders = []
    with httpx.Client(timeout=30) as client:
        for batch in range(24):
            start = time.monotonic()
            for i in range(batch * 2, batch * 2 + 2):
                payload = {"items": [{"productId": str(i % 20 + 1), "quantity": i % 3 + 1},
                                     {"productId": str((i + 7) % 20 + 1), "quantity": 1}],
                           "customerName": f"Демо-покупатель {i % 12 + 1:02d}",
                           "address": f"Вымышленный адрес: Демо-город, Учебная улица, дом {i % 12 + 1}"}
                r = client.post("http://localhost:18102/api/orders", json=payload,
                                headers={"Idempotency-Key": f"infraaxon-demo-v1-{i:03d}"})
                r.raise_for_status()
                orders.append(r.json()["id"])
            for page in (1, 2, 1):
                client.get("http://localhost:18101/api/products", params={"page": page}).raise_for_status()
            client.get(f"http://localhost:18101/api/products/{batch % 20 + 1}/image").raise_for_status()
            if batch % 6 == 5:
                print(f"Traffic: {len(orders)}/48 demo orders submitted", flush=True)
            time.sleep(max(0, 5 - (time.monotonic() - start)))
        pending = set(orders)
        deadline = time.monotonic() + 120
        while pending and time.monotonic() < deadline:
            for oid in list(pending):
                r = client.get(f"http://localhost:18102/api/orders/{oid}")
                r.raise_for_status()
                if r.json()["status"] == "completed":
                    pending.remove(oid)
            if pending:
                time.sleep(2)
        if pending:
            raise RuntimeError(f"{len(pending)} demonstration orders have not completed")
    print("Traffic: all 48 demonstration orders completed", flush=True)
    return {"demo_orders": len(orders), "completed": len(orders)}


def collaboration():
    conf = configuration()
    with httpx.Client(timeout=60) as client:
        def gql(query, variables=None, token=None):
            r = client.post("http://localhost:18084/graphql", json={"query": query, "variables": variables or {}},
                            headers={"Authorization": "Bearer " + token} if token else {})
            r.raise_for_status()
            result = r.json()
            if result.get("errors"):
                raise RuntimeError("Wiki.js GraphQL operation failed")
            return result["data"]
        auth = gql('mutation($p:String!){authentication{login(username:"admin@infraaxon.local",password:$p,strategy:"local"){jwt responseResult{succeeded}}}}',
                   {"p": conf["WIKIJS_ADMIN_PASSWORD"]})["authentication"]["login"]
        if not auth["responseResult"]["succeeded"]:
            raise RuntimeError("Wiki.js login failed")
        token = auth["jwt"]
        existing = {p["path"] for p in gql("{pages{list(limit:1000){id path}}}", token=token)["pages"]["list"]}
        pages = list(PAGES)
        pages.append(("home", "Добро пожаловать в InfraAxon demo", "# Учебная инфраструктура\n\nНачните с [обзора стенда](/en/demo/overview).\n\n" + "\n".join(f"- [{title}](/en/{path})" for path, title, _ in PAGES)))
        for path, title, content in pages:
            if path not in existing:
                result = gql('mutation($path:String!,$title:String!,$content:String!){pages{create(path:$path,title:$title,content:$content,description:"Синтетический учебный материал InfraAxon",editor:"markdown",isPublished:true,isPrivate:false,locale:"en",tags:["infraaxon","demo"]){responseResult{succeeded message}}}}',
                             {"path": path, "title": title, "content": content}, token)["pages"]["create"]
                if not result["responseResult"]["succeeded"]:
                    raise RuntimeError(result["responseResult"]["message"])
        print("Wiki.js: 7 demonstration pages available", flush=True)

        response = client.post("http://localhost:18085/api/v4/users/login", json={"login_id": "infraaxon-admin", "password": conf["MATTERMOST_ADMIN_PASSWORD"]})
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.headers["Token"]}
        team = client.get("http://localhost:18085/api/v4/teams/name/infraaxon", headers=headers).json()
        channel = client.get(f"http://localhost:18085/api/v4/teams/{team['id']}/channels/name/incidents", headers=headers).json()
        posts = client.get(f"http://localhost:18085/api/v4/channels/{channel['id']}/posts?per_page=200", headers=headers).json()["posts"]
        messages = [
            "Добро пожаловать в учебный стенд. Все покупатели и заказы вымышлены. Обзор: http://localhost:18084/en/demo/overview",
            "Каталог: 20 товаров в трёх категориях. Изображения хранятся в MinIO/products. Магазин: http://localhost:18081",
            "Набор заказов: 48 оформлений через Orders.Api → MongoDB outbox → Kafka → Worker. Ожидаемый конечный статус: completed.",
            "Redis: catalog:products — кеш на 30 секунд. Учебные hash и sorted set находятся под префиксом infraaxon:demo:.",
            "Исторический пример Redis: каталог продолжал работать через MongoDB. Проверяли путь Toxiproxy и предупреждения кеша. Это учебный пример, не текущая авария.",
            "Исторический пример Kafka: рост lag был следствием медленных операций MongoDB в Worker. Прежде чем обвинять брокер, проверяйте downstream. Это синтетическая история.",
            "Исторический пример MinIO: список товаров доступен, изображения не читаются. Проверки: bucket, key, права и сетевой путь. Текущий отказ не включён.",
            "PostgreSQL: SELECT * FROM infraaxon_demo.inventory; В схеме также есть customers и снимок демонстрационных orders.",
            "MinIO/infraaxon-demo: exports/inventory.csv, exports/customers.json, reports/orders.json и инструкции в runbooks/.",
            "Grafana: http://localhost:13000/d/infraaxon — completed orders, Kafka lag, cache hits/misses. Временной диапазон: последние 30 минут.",
            "Kibana: infraaxon-logs и infraaxon-traces — реальные наблюдения. infraaxon-demo-events — 180 явно помеченных синтетических учебных событий.",
            "Задачи и улучшения стенда: http://localhost:18083/projects/infraaxon-demo/work_packages . Инструкции: http://localhost:18084/en/demo/observability",
        ]
        for i, message in enumerate(messages, 1):
            marker = f"[InfraAxon demo v1/{i:02d}]"
            if not any(p["message"].startswith(marker) for p in posts.values()):
                client.post("http://localhost:18085/api/v4/posts", headers=headers,
                            json={"channel_id": channel["id"], "message": marker + " " + message}).raise_for_status()
        print("Mattermost: 12 demonstration messages available", flush=True)

    ruby = r'''
require 'json'
admin=User.find_by!(login:'admin'); User.current=admin
project=Project.find_by!(identifier:'infraaxon-demo')
subjects=[
 ['Подготовить витрину из 20 товаров','Проверить категории, цены и изображения в MinIO.','Closed'],
 ['Проверить жизненный цикл демонстрационных заказов','48 заказов проходят MongoDB outbox, Kafka и Worker.','Closed'],
 ['Добавить обзор метрик магазина','Показать completed orders, consumer lag и cache hit rate.','In progress'],
 ['Описать резервное чтение каталога','Документировать fallback Redis → MongoDB и проверку сетевого пути.','Closed'],
 ['Подготовить выгрузку остатков','CSV в MinIO/infraaxon-demo и таблица infraaxon_demo.inventory.','Closed'],
 ['Сверить снимок заказов PostgreSQL','Сопоставить число заказов, статусы и сумму с MongoDB.','In testing'],
 ['Учебный разбор: Redis недоступен','Синтетическая история: каталог доступен через MongoDB. Это не текущий инцидент.','Closed'],
 ['Учебный разбор: рост Kafka lag','Синтетическая история: медленная база в Worker увеличивает lag.','Closed'],
 ['Учебный разбор: изображения S3','Синтетическая история: метаданные есть, объект недоступен.','Closed'],
 ['Добавить наблюдение за остатками','Предложение: сигнализировать о низком остатке тестовых товаров.','New'],
 ['Подготовить памятку оператора','Ссылки на интерфейсы, маршруты диагностики и примеры запросов.','In progress'],
 ['Проверить повторяемость наполнения','Повторный запуск не должен дублировать заказы и учебные материалы.','In testing']
]
subjects.each_with_index do |(title,description,status),i|
 subject="[DEMO] #{title}"
 next if project.work_packages.exists?(subject:subject)
 WorkPackage.create!(project:project,subject:subject,description:"Учебные данные InfraAxon. #{description}\n\nБаза знаний: http://localhost:18084/en/demo/overview",author:admin,
   type:Type.find_by!(name:'Task'),status:Status.find_by!(name:status),priority:IssuePriority.first,
   start_date:Date.today-7+i/2,due_date:Date.today+2+i,estimated_hours:2+i%4)
end
puts "DEMO_WORK_PACKAGES=#{project.work_packages.where('subject LIKE ?', '[DEMO]%').count}"
'''
    result = subprocess.run(["docker", "exec", "-i", "-u", "app", "infraaxon-shop-openproject-1", "bundle", "exec", "rails", "runner", "-"], input=ruby, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("OpenProject data population failed: " + result.stderr[-1800:])
    print(next(line for line in result.stdout.splitlines() if line.startswith("DEMO_WORK_PACKAGES=")), flush=True)
    return {"wiki_demo_pages": len(pages), "mattermost_demo_posts": len(messages), "openproject_demo_tasks": 12}


def views():
    with httpx.Client(base_url="http://localhost:15601", headers={"kbn-xsrf": "demo", "Content-Type": "application/json"}, timeout=30) as client:
        for id, title, name in [("infraaxon-demo-events", "infraaxon-demo-events", "Учебные события — SYNTHETIC DEMO"),
                                ("infraaxon-live-logs", "infraaxon-logs", "Живые журналы магазина"),
                                ("infraaxon-live-traces", "infraaxon-traces", "Живые трассировки магазина")]:
            client.post("/api/data_views/data_view", json={"data_view": {"id": id, "title": title, "name": name, "timeFieldName": "@timestamp"}, "override": True}).raise_for_status()
        client.post("/api/data_views/default", json={"data_view_id": "infraaxon-live-logs", "force": True}).raise_for_status()
    print("Kibana: live logs, live traces and synthetic events have separate data views", flush=True)


def main():
    subprocess.run(["python3", str(ROOT / "tools/bootstrap_shop.py")], check=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        traffic_job = pool.submit(traffic)
        collaboration_job = pool.submit(collaboration)
        result = {**traffic_job.result(), **collaboration_job.result()}
    conf = configuration()
    keys = ["POSTGRES_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"]
    cmd = ["docker", "exec", "-i"]
    for key in keys:
        cmd.extend(["-e", key])
    cmd.extend(["infraaxon-platform-1", "python", "-"])
    native = subprocess.run(cmd, input=(ROOT / "tools/populate_demo_native.py").read_text(), text=True,
                            capture_output=True, env={**os.environ, **{k: conf[k] for k in keys}})
    if native.returncode:
        raise RuntimeError("Native data population failed: " + native.stderr[-1800:])
    result.update(json.loads(native.stdout.strip()))
    views()
    env = next(e for e in request("/environments") if e["name"] == "Shop demo")
    components = request(f"/environments/{env['id']}/components")
    for c in components:
        request(f"/components/{c['id']}/check", {}, "POST")
    result["components_checked"] = len(components)
    output = ROOT / "artifacts/demo-data.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
