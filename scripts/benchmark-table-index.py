"""Synthetic benchmark; never opens the merchant database or browser."""

import json
import statistics
import tempfile
import time
from backend.store import Store
from backend.business import filter_rows
from backend.table_sort import sort_rows
from backend import table_index

with tempfile.TemporaryDirectory() as directory:
    store = Store(directory)
    sid = store.add("benchmark", "fixture")
    shop = store.list()[0]
    rows = [
        {
            "id": str(i),
            "goods_id": str(i),
            "name": f"测试商品{i}",
            "sales": (i * 317) % 50000,
            "stock": i % 30,
            "status": "在售中",
            "created_at": "2026-10-09",
            "thumbnail_url": "https://example.com/product.png",
        }
        for i in range(50000)
    ]
    store.write_cache(sid, "products:benchmark", {"rows": rows})

    def old():
        saved = store.read_cache(sid, "products:benchmark")["payload"]["rows"]
        from backend.product_images import enrich

        saved = enrich(saved, store.product_images(sid))
        saved = filter_rows(saved, "products", status="在售中")
        saved.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return sort_rows(saved, "sales", "desc")[20:40]

    def new():
        return table_index.query(
            store,
            [(shop, "products:benchmark")],
            "products",
            status="在售中",
            sort_by="sales",
            sort_direction="desc",
            page=2,
        )["rows"]

    assert [r["id"] for r in old()] == [r["id"] for r in new()]
    result = {"rows": len(rows), "page_size": 20, "runs": 5}
    for name, fn in [("previous_ms", old), ("indexed_ms", new)]:
        values = []
        for _ in range(5):
            start = time.perf_counter()
            fn()
            values.append((time.perf_counter() - start) * 1000)
        result[name] = round(statistics.median(values), 1)
    result["speedup"] = round(result["previous_ms"] / result["indexed_ms"], 1)
    print(json.dumps(result))
