"""Canonical reads, fixed subscriptions and deterministic per-shop staggering."""

import hashlib
from datetime import datetime, timedelta
from .business_reader import BEIJING

HOME = {"overview", "transactions"}


def core(kind, options):
    """Scopes counted as today's core coverage (products are separate)."""
    return (
        bool(options.get("_attention"))
        or kind in ("overview", "transactions", "ad_reports")
        and options.get("trade_range", "realtime") == "realtime"
        and not options.get("_exact_dates")
    )


# These adapters can read all filter values and the database applies the UI filter.
MERGED = {"reviews", "tickets", "packages", "invoices", "evidence", "intercepts", "quality"}


def canonical(kind, options, platform="pdd"):
    result = dict(options)
    if kind == "tickets" and options.get("status") == "待处理":
        return result
    if options.get("_filtered_read") or options.get("_attention") or options.get("_invoice_details_refresh"):
        return result
    if options.get("_exact_dates"):
        result.update(status="", stars=[], reply="")
        return result
    mergeable = (
        kind in MERGED or kind == "aftersales" and options.get("status", "") in ("", "平台介入待商家处理", "平台处理中")
    )
    if platform != "pdd" or not mergeable:
        return result
    result.update(status="", stars=[], reply="")
    # Only widen windows when the local row date has the same meaning as the API date.
    if kind in ("reviews", "tickets", "packages", "evidence"):
        from .business import options_for

        default = options_for(kind)
        if result.get("date_from", "") >= default["date_from"] and result.get("date_to", "9999") <= default["date_to"]:
            result.update({k: default[k] for k in ("start", "end", "date_from", "date_to", "explicit_dates")})
            result["_relative_days"] = default["_relative_days"]
    return result


def obsolete_realtime(kind, options):
    if kind == "orders" and options.get("_order_lane"):
        lane = options["_order_lane"]
        today = datetime.now(BEIJING).date()
        if lane == "open" and options.get("_order_partition") != 2:
            return True
        floor = today - timedelta(
            days=1
            if lane == "recent"
            else 2
            if lane == "open" and options.get("_order_partition") == 2
            else 0
            if lane == "open"
            else 89
        )
        return bool(options.get("date_to") and options["date_to"] < floor.isoformat())
    # Default rolling order tabs from past days must not retry expired windows.
    if (
        kind == "orders"
        and not options.get("_order_lane")
        and not options.get("explicit_dates")
        and not options.get("_exact_dates")
    ):
        return bool(options.get("date_to") and options["date_to"] < datetime.now(BEIJING).date().isoformat())
    if kind not in ("transactions", "goodsdata", "ad_products", "ad_reports"):
        return False
    if options.get("trade_range") != "realtime":
        from .history_frequency import historical

        if not historical(kind, options) or options.get("_exact_dates") or options.get("explicit_dates"):
            return False
        # Native relative selectors roll at midnight. Never resume their new
        # pages under yesterday's task identity; current subscriptions replace it.
        today = datetime.now(BEIJING).date()
        date = options.get("day") or options.get("date_to")
        cutoff = today if options.get("day") else today - timedelta(days=1)
        return bool(date and date < cutoff.isoformat())
    date = options.get("day") or options.get("date_to")
    return bool(date and date < datetime.now(BEIJING).date().isoformat())


def stagger(shop_id, kind, seconds=60):
    return int(hashlib.sha256(f"{shop_id}:{kind}".encode()).hexdigest()[:8], 16) % max(1, seconds)


def priority(kind):
    return 0 if kind in HOME else 1 if kind in ("aftersales", "evidence", "tickets") else 2


def midnight_home(kind, options, last_success, now=None):
    current = now or datetime.now(BEIJING)
    return (
        current.hour < 2
        and kind in ("overview", "transactions", "ad_reports")
        and not options.get("_exact_dates")
        and options.get("trade_range", "realtime") == "realtime"
        and (not last_success or datetime.fromtimestamp(last_success, BEIJING).date() < current.date())
    )


def key_scope(kind, options, platform="pdd"):
    scope = dict(options)
    if (
        platform == "pdd"
        and scope.get("_relative_days")
        and not scope.get("_exact_dates")
        and not scope.get("explicit_dates")
    ):
        for key in ("start", "end", "date_from", "date_to"):
            scope.pop(key, None)
        # Undated invoice reads cover every age, independently of the displayed window.
        if kind == "invoices":
            scope["_relative_days"] = "all"
    else:
        scope.pop("_relative_days", None)
    return scope


def roll_window(options):
    days = options.get("_relative_days")
    if not days or options.get("_exact_dates") or options.get("explicit_dates"):
        return options
    from datetime import timedelta

    today = datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - timedelta(days=int(days) - 1)
    return {
        **options,
        "start": int(first.timestamp()),
        "end": int((today + timedelta(days=1)).timestamp()) - 1,
        "date_from": first.date().isoformat(),
        "date_to": today.date().isoformat(),
    }
