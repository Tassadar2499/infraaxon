"""Optional shop domain specialization, separate from generic infrastructure profiles."""

from .profiles import profile

SHOP_PROFILES = {
    p["id"]: p
    for p in [
        profile(
            "shop-catalog",
            "http",
            "Catalog / каталог товаров",
            "Отвечаешь за чтение каталога из MongoDB, Redis-кэш с TTL 30 секунд и изображения MinIO. Redis недоступен: возможен fallback в MongoDB. /health проверяет только процесс. Сопоставляй GET каталога, ошибки зависимостей, cache hit/miss и источник Images.",
            "catalog каталог товары поиск cache кэш кеш изображения медленно",
            ("inspect", "check"),
            [{"action": "inspect"}, {"action": "check", "check_name": "products"}],
            {"service_name": "shop-catalog", "checks": {"products": "/api/products?page=1"}},
        ),
        profile(
            "shop-orders",
            "http",
            "Orders / приём и outbox",
            "Отвечаешь за приём заказов, серверную проверку цен и durable outbox в MongoDB. Различай успешный приём, публикацию Kafka и завершение worker. Сопоставляй размер и возраст outbox, ошибки публикации, Catalog/MongoDB/Kafka. Не создавай заказы для диагностики.",
            "orders заказы заказ checkout оформление outbox публикация цены",
            ("inspect", "check"),
            [{"action": "inspect"}, {"action": "check", "check_name": "outbox"}],
            {"service_name": "shop-orders", "checks": {"outbox": "/internal/diagnostics"}},
        ),
        profile(
            "shop-worker",
            "http",
            "Worker / обработка заказов",
            "Отвечаешь за consumer shop-worker топика orders.created, обработку и статус заказа в MongoDB. Старый/null lastProcessedAt без lag и входящих заказов не доказывает зависание. Сопоставляй lag, прогресс обработки и ошибки; повторная доставка допустима. Если скорость завершения > 0 или lastProcessedAt попадает в окно, нельзя утверждать отсутствие заказов во всём окне. Пустой поиск логов означает отсутствие найденных записей, а не заказов. Исторические обсуждения MongoDB не подтверждают текущую задержку. Не читай управление сценариями.",
            "worker обработчик обработка заказы зависли очередь lag consumer",
            settings={"service_name": "shop-worker"},
        ),
        profile(
            "shop-storefront",
            "http",
            "Storefront / витрина",
            "Проверяй HTML, локальные ресурсы и GET каталога через nginx. Сопоставляй с Catalog и Orders; HTML 200 не доказывает работу браузера или оформления заказа. Не создавай заказы.",
            "storefront витрина магазин frontend страница товары браузер",
            ("inspect", "check"),
            [
                {"action": "inspect"},
                {"action": "check", "check_name": "assets"},
                {"action": "check", "check_name": "products"},
            ],
            {"health_path": "/", "checks": {"assets": "/", "products": "/api/products?page=1"}},
        ),
    ]
}

# v2 clarifies idle-window interpretation after a recorded real-model evaluation.
SHOP_PROFILES["shop-worker"]["version"] = 2
