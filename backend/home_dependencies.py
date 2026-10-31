"""Homepage coverage scopes share the task catalog and scheduled read identities."""

from datetime import datetime, timedelta
from .business_reader import BEIJING
from .task_catalog import HOME
from .platform_support import supports


def _at(options, current):
    """Anchor only homepage rolling windows to the one coverage request clock."""
    options = dict(options)
    today = current.date()
    if "day" in options:
        options["day"] = today.isoformat()
    if options.get("date_from") and options.get("date_to"):
        span = datetime.fromisoformat(options["date_to"]).date() - datetime.fromisoformat(options["date_from"]).date()
        first = today - span
        options.update(date_from=first.isoformat(), date_to=today.isoformat())
        if "start" in options:
            options["start"] = int(datetime.combine(first, datetime.min.time(), tzinfo=BEIJING).timestamp())
        if "end" in options:
            options["end"] = (
                int(datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING).timestamp()) - 1
            )
    return options


def scopes(shop, at=None):
    from .business import options_for
    from . import attention, promotions, transactions
    from .frequency_policy import task_id
    from .order_plan import live
    from .export_labels import MODULES

    current = at or datetime.now(BEIJING)
    platform = shop.get("platform", "pdd")
    focused = {task_id(kind, options): options for kind, options in attention.scopes(shop)}
    orders = {task_id("orders", options): options for options in live()} if platform == "pdd" else {}
    titles = {
        "overview@base": "经营概览",
        "transactions@base": "交易 / 走势",
        "ad_reports@base": "推广花费 / ROI",
        "products@base": "每日 / 每月上新",
        "orders@refund": "今日退款订单 / 金额",
        "orders@shipping_due": "即将逾期发货",
        "aftersales@expiring": "售后过期预警",
    }
    for identifier in sorted(HOME):
        kind, category = identifier.split("@", 1)
        if not supports(platform, kind):
            continue
        if category == "attention":
            options = focused.get(identifier)
        elif kind == "orders":
            options = orders.get(identifier)
        elif identifier == "aftersales@expiring":
            # The native imminent-expiry selector is currently implemented only for PDD.
            options = options_for(kind, status="待处理即将逾期") if platform == "pdd" else None
        elif category == "base":
            options = (
                transactions.options_for("realtime")
                if kind == "transactions"
                else promotions.options_for()
                if kind == "ad_reports"
                else options_for(kind)
            )
        else:
            options = None
        if options is not None:
            title = titles.get(identifier, "重点 · " + MODULES.get(kind, kind))
            yield identifier, kind, _at(options, current), title
