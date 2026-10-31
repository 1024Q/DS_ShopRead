import asyncio, json, sys
from pathlib import Path
from backend.store import Store
from backend.browser import Session
from backend.business import options_for, BusinessService
from backend.business_reader import collect


async def main():
    store = Store()
    for record in store.list():
        session = Session(record, store)
        session.state = "logged_in"
        service = BusinessService(store, lambda _: session)
        for kind in sys.argv[1:] or [
            "brands",
            "navigator",
            "quality",
            "evidence",
            "intercepts",
            "invoices",
            "packages",
            "aftersales",
        ]:
            opts = options_for(kind, status="待开票" if kind == "invoices" else "")
            try:
                result = await asyncio.wait_for(collect(session, kind, opts), 240)
                store.write_cache(record["id"], service.key(kind, opts, record), result)
                Path(f".data/live-{record['id']}-{kind}.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf8"
                )
                print(
                    json.dumps(
                        {
                            "shop": record["id"],
                            "kind": kind,
                            "rows": len(result["rows"]),
                            "total": result["source_total"],
                            "complete": result["complete"],
                            "statuses": list({r.get("status") for r in result["rows"]}),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as error:
                print(record["id"], kind, type(error).__name__, str(error), flush=True)


asyncio.run(main())
