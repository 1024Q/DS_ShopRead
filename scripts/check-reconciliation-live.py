"""Read current published statements and check monthly/daily agreement."""

import asyncio
from backend.store import Store
from backend.browser import Session
from backend.business_reader import collect
from backend.business import BusinessService
from backend.reconciliation import options_for, summarize


async def main():
    store = Store()
    service = BusinessService(store, None)
    for record in store.list():
        session = Session(record, store)
        results = {}
        for kind in ("reconciliation_day", "reconciliation_month"):
            options = options_for(kind)
            result = await collect(session, kind, options)
            store.write_cache(record["id"], service.key(kind, options, record), result)
            results[kind] = result
            print(
                record["id"], kind, "rows=", len(result["rows"]), "through=", result["latest_summary_day"], flush=True
            )
        daily = results["reconciliation_day"]["rows"]
        monthly = next(
            (
                row
                for row in results["reconciliation_month"]["rows"]
                if row["period"] == options_for("reconciliation_day")["period"]
            ),
            None,
        )
        if daily and monthly:
            sums = summarize(daily)
            for field in ("income", "outcome", "net", "income_count", "outcome_count"):
                assert sums[field] == monthly[field], f"Daily/monthly mismatch: {field}"
            assert min(daily, key=lambda r: r["period"])["opening_balance_cents"] == monthly["opening_balance_cents"]
            assert max(daily, key=lambda r: r["period"])["closing_balance_cents"] == monthly["closing_balance_cents"]
            print(record["id"], "Daily/monthly amounts, counts and balances match", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
