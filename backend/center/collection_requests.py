"""Versioned collection intent; unknown legacy requests are never discarded.

Metadata lives in the existing JSON request so code rollback needs no schema
migration. Old agents ignore the extra query argument; upgraded agents strip it.
"""

import json
import time

META = "_dispatch"
AUTOMATIC_TTL = 300
RELATIVE_REPORTS = {"transactions", "goodsdata", "ad_products", "ad_reports"}
QUERY_FIELDS = {
    "shop_id",
    "date_from",
    "date_to",
    "status",
    "stars",
    "reply",
    "period",
    "trade_range",
    "platform",
    "exact_dates",
}


def build(kind, options, shop_id, force=False, source="manual", now=None):
    now = time.time() if now is None else now
    request = {"kind": kind, "shop_id": shop_id}
    for key in ("status", "reply", "period", "trade_range"):
        if options.get(key):
            request[key] = options[key]
    if options.get("stars"):
        request["stars"] = ",".join(map(str, sorted(set(options["stars"]))))

    # Native relative selectors must be resolved when dispatched. Sending their
    # generated dates makes rolling order/aftersale scopes explicit historical
    # reads, and old order ranges may even fail the platform's 90-day validation.
    relative = not options.get("_exact_dates") and not options.get("explicit_dates")
    if relative and kind not in RELATIVE_REPORTS and not options.get("_relative_days"):
        from ..business import options_for

        default = options_for(
            kind, status=options.get("status", ""), stars=request.get("stars", ""), reply=options.get("reply", "")
        )
        relative = all(not options.get(key) or options[key] == default.get(key) for key in ("date_from", "date_to"))
    if not relative:
        for key in ("date_from", "date_to"):
            if options.get(key):
                request[key] = options[key]
    if options.get("_exact_dates"):
        request["exact_dates"] = True

    # User-selected historical ranges/periods are durable queries even when a
    # missing-data GET created the request. Their checkpoints must not expire.
    if source == "automatic" and (not relative or options.get("period")):
        source = "historical"
    request[META] = {
        "version": 1,
        "source": source,
        "force": bool(force),
        "generated_at": now,
        "valid_until": now + AUTOMATIC_TTL if source == "automatic" else 0,
    }
    return request


def metadata(request):
    value = request.get(META)
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


def expired(request, now=None):
    value = metadata(request)
    deadline = value.get("valid_until")
    return (
        value.get("source") == "automatic"
        and isinstance(deadline, (int, float))
        and deadline > 0
        and deadline <= (time.time() if now is None else now)
    )


def identity(request):
    """Equivalent intent coalesces despite renewed timestamps or empty fields."""
    value = metadata(request)
    if not value:
        return json.dumps(request, sort_keys=True, ensure_ascii=False)
    scope = {k: v for k, v in request.items() if k != META and v not in ("", None)}
    if scope.get("stars"):
        scope["stars"] = ",".join(sorted(set(str(scope["stars"]).split(","))))
    scope[META] = {k: value.get(k) for k in ("version", "source", "force")}
    return json.dumps(scope, sort_keys=True, ensure_ascii=False)


def params(request):
    value = metadata(request)
    # Preserve the legacy/manual force behavior only for unversioned requests.
    force = bool(value.get("force")) if value else True
    return {**{k: v for k, v in request.items() if k in QUERY_FIELDS}, "force": "true" if force else "false"}


async def dispatch(local, request):
    if expired(request):
        return {"ok": True, "skipped": True, "message": "自动采集需求已过期，等待当前范围需求；未修改采集断点"}
    if request.get("_list_completion", {}).get("version") == 1:
        result = await local.post(
            f"/api/data/{request['kind']}/complete-once",
            json={
                "request_id": request["_list_completion"]["id"],
                "shop_ids": [request["shop_id"]],
                "status": request["status"],
            },
        )
    else:
        result = await local.post(f"/api/data/{request['kind']}/sync", params=params(request))
    result.raise_for_status()
    return {"ok": True, "message": "已核对本机采集需求，成功结果将单独回传"}
