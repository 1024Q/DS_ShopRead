"""Daily and monthly new goods by creation date in Beijing time."""

from datetime import date, timedelta


def summarize(data, day, year=None, operator_names=()):
    selected = date.fromisoformat(day)
    previous = (selected - timedelta(days=1)).isoformat()
    year = year or selected.year
    counts = []
    for source in data["sources"]:
        rows = {str(r.get("goods_id") or r["id"]): r for r in data["rows"] if r["shop_id"] == source["shop_id"]}
        unknown = sum(not r.get("created_at") for r in rows.values())
        ready = bool(source.get("updated_at"))
        projection = source.get("_creation_counts")
        if projection is not None:
            unknown = projection["unknown"]
        counter = lambda prefix: (
            (
                sum(n for d, n in projection["days"].items() if d.startswith(prefix))
                if projection is not None
                else sum(str(r.get("created_at", "")).startswith(prefix) for r in rows.values())
            )
            if ready
            else None
        )
        counts.append(
            {
                **source,
                "count": counter(day),
                "previous": counter(previous),
                "unknown_dates": unknown,
                "days": {
                    d: counter(d)
                    for d in sorted(
                        projection["days"]
                        if projection is not None
                        else {str(r.get("created_at", ""))[:10] for r in rows.values()}
                    )
                    if d.startswith(day[:7])
                },
                "months": {f"{year}-{m:02d}": counter(f"{year}-{m:02d}") for m in range(1, 13)},
            }
        )
    available = [s for s in counts if s["count"] is not None]
    total = lambda key: sum(s[key] for s in available) if available else None
    operators = {
        name: {
            "name": name,
            "shops": [],
            "ready": 0,
            "count": 0,
            "previous": 0,
            "months": {f"{year}-{m:02d}": 0 for m in range(1, 13)},
        }
        for name in operator_names
    }
    for shop in counts:
        for name in shop.get("operators") or ["未分配运营"]:
            group = operators.setdefault(
                name,
                {
                    "name": name,
                    "shops": [],
                    "ready": 0,
                    "count": None,
                    "previous": None,
                    "months": {f"{year}-{m:02d}": None for m in range(1, 13)},
                },
            )
            if not group["shops"]:
                group.update(count=None, previous=None, months={f"{year}-{m:02d}": None for m in range(1, 13)})
            group["shops"].append(shop["shop_id"])
            group.setdefault("days", {})
            if shop["count"] is not None:
                group["ready"] += 1
                for key in ("count", "previous"):
                    group[key] = (group[key] or 0) + shop[key]
                for period, value in shop["months"].items():
                    group["months"][period] = (group["months"][period] or 0) + value
                for date_key, value in shop["days"].items():
                    group["days"][date_key] = group["days"].get(date_key, 0) + (value or 0)
    return {
        "operators": sorted(operators.values(), key=lambda r: r["name"]),
        "date": day,
        "previous_date": previous,
        "year": year,
        "total": total("count"),
        "previous_total": total("previous"),
        "shops": counts,
        "monthly": [
            {
                "period": f"{year}-{m:02d}",
                "count": sum(s["months"][f"{year}-{m:02d}"] for s in available) if available else None,
            }
            for m in range(1, 13)
        ],
        "ready": len(available),
        "complete": bool(counts)
        and len(available) == len(counts)
        and all(s.get("complete") and not s["unknown_dates"] for s in available),
        "sources": data["sources"],
        "rows": [],
        "metrics": [],
    }
