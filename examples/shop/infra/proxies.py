import time
import httpx
for attempt in range(40):
    try:
        r=httpx.get("http://toxiproxy:8474/proxies",timeout=3); r.raise_for_status();break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("Toxiproxy did not become ready")
for name, port, upstream in [("mongo",18666,"mongo:27017"),("redis",16379,"redis:6379"),("minio",19000,"minio:9000"),("kafka",19092,"kafka:9092"),("prometheus",19091,"prometheus:9090"),("elasticsearch",19200,"elasticsearch:9200")]:
    existing=httpx.get(f"http://toxiproxy:8474/proxies/{name}")
    if existing.status_code==404:
        r=httpx.post("http://toxiproxy:8474/proxies",json={"name":name,"listen":f"0.0.0.0:{port}","upstream":upstream})
        r.raise_for_status()
print("Proxy paths ready")
