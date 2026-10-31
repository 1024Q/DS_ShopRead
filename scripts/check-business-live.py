import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from backend.store import Store
from backend.browser import Session
from backend.business_reader import collect
from backend.business import options_for


async def main():
    store = Store()
    for record in store.list():
        item = Session(record, store)
        item.state = "logged_in"
        for kind in ("overview", "products", "tickets", "reviews"):
            try:
                options = options_for(
                    kind, stars="1,2,3" if kind == "reviews" else "", reply="unreplied" if kind == "reviews" else ""
                )
                async with item.lock:
                    data = await collect(item, kind, options)
                print(
                    json.dumps(
                        {
                            "shop_id": record["id"],
                            "kind": kind,
                            "rows": len(data.get("rows", [])),
                            "source_total": data.get("source_total"),
                            "complete": data.get("complete"),
                            "metrics": data.get("metrics"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                Path(f".data/live-{record['id']}-{kind}.json").write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf8"
                )
            except Exception as error:
                print(record["id"], kind, type(error).__name__, str(error), flush=True)


asyncio.run(main())
