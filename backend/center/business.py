"""Business queries use PostgreSQL snapshots and the existing filtering/export contract."""

import contextvars
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import select, tuple_
from .database import snapshots
from ..business import BusinessService
from ..business_reader import BEIJING
from ..filter_scope import matches
from ..operations import decorate
from ..platform_support import supports
from ..shop_identity import identity

selected_node = contextvars.ContextVar("center_node", default=None)


class CentralStore:
    def __init__(self, database):
        self.database = database
        self.path = Path(".data/center/exports-placeholder")

    def list(self):
        node = selected_node.get()
        return [s for s in self.database.shops() if not node or s["node_id"] == node]

    def operators(self):
        return sorted({name for s in self.list() for name in s.get("operators", [])})

    def company_names(self):
        result = {}
        newest = {}
        by_key = {(s["node_id"], str(s["local_id"])): s["id"] for s in self.list()}
        for chunk in chunks(list(by_key)):
            with self.database.engine.connect() as db:
                for row in db.execute(
                    select(snapshots.c.node, snapshots.c.shop, snapshots.c.payload, snapshots.c.updated).where(
                        tuple_(snapshots.c.node, snapshots.c.shop).in_(chunk), snapshots.c.scope.like("tax:%")
                    )
                ).mappings():
                    shop = by_key[(row["node"], row["shop"])]
                    name = (row["payload"].get("entity") or {}).get("company_name")
                    if name and row["updated"] > newest.get(shop, -1):
                        result[shop] = name
                        newest[shop] = row["updated"]
        return result


def chunks(values, size=300):
    return [values[start : start + size] for start in range(0, len(values), size)]


def load(database, keys, patterns=()):
    """Snapshots for many shops in one query per chunk: exact (node,shop,scope) keys plus per-shop scope prefixes."""
    rows = []
    with database.engine.connect() as db:
        for chunk in chunks(sorted(set(keys))):
            rows.extend(
                db.execute(
                    select(snapshots).where(tuple_(snapshots.c.node, snapshots.c.shop, snapshots.c.scope).in_(chunk))
                ).mappings()
            )
        for prefix, pairs in patterns:
            for chunk in chunks(sorted(set(pairs))):
                rows.extend(
                    db.execute(
                        select(snapshots).where(
                            tuple_(snapshots.c.node, snapshots.c.shop).in_(chunk),
                            snapshots.c.scope.like(prefix.replace("_", r"\_") + "%", escape="\\"),
                        )
                    ).mappings()
                )
    from .snapshot_freshness import overlay

    rows = overlay(database, rows)
    grouped = {}
    for row in rows:
        grouped.setdefault((row["node"], row["shop"]), []).append(row)
    return grouped


class CentralBusiness:
    def __init__(self, database):
        self.database = database
        self.store = CentralStore(database)
        from .home_counts import CountCache

        self._home_count_cache = CountCache()

    def home_counts(self, kind, options, shop_id=None, platform=None, filters=None, page_size=20):
        from .home_counts import read

        return read(self, kind, options, shop_id, platform, filters or {}, page_size)

    def key(self, kind, options, record):
        return BusinessService.key(self, kind, options, record)

    def request_sync(self, kind, options, shop_id=None, force=False, platform=None, shops=None, source="manual"):
        from .collection_requests import build

        tasks = []
        for shop in self.store.list() if shops is None else shops:
            if (
                not matches(shop_id, shop["id"])
                or not matches(platform, shop.get("platform", "pdd"))
                or not supports(shop.get("platform", "pdd"), kind)
            ):
                continue
            request = build(kind, options, shop["local_id"], force, source)
            job = self.database.enqueue(shop["node_id"], request)
            tasks.append({"shop_id": shop["id"], "job_id": job, "syncing": True})
        return {"tasks": tasks, "mode": "database", "force": force}

    def data(self, kind, options, shop_id=None, refresh=False, force=False, platform=None, read_only=False, **unused):
        rows = []
        sources = []
        metrics = []
        missing = []
        from .freshness import configurations, describe

        policies = configurations(self.database)
        selected = [
            shop
            for shop in self.store.list()
            if matches(shop_id, shop["id"])
            and matches(platform, shop.get("platform", "pdd"))
            and supports(shop.get("platform", "pdd"), kind)
        ]
        focused = kind in ("aftersales", "evidence", "tickets", "intercepts", "packages", "invoices")
        wanted = {}
        from ..summary_publication import KINDS as SUMMARY_KINDS, key as summary_key, merge as merge_summary

        for shop in selected:
            key = self.key(kind, options, shop)
            keys = [key, "archive:" + key]
            if kind == "orders" and shop.get("platform", "pdd") == "pdd":
                from ..order_plan import view

                if view(options):
                    keys.extend(
                        [
                            "orders:view-v1:" + str(shop.get("profile_version", 0)),
                            BusinessService.key(self, kind, options, shop, legacy=True),
                        ]
                    )
                if options.get("status") == "待发货":
                    from ..list_completion import order_key

                    keys.append(order_key(shop))
            if kind == "invoices":
                keys.append(self.key(kind, {**options, "_invoice_details_refresh": True}, shop))
            wanted[shop["id"]] = keys
        exact = [
            (s["node_id"], str(s["local_id"]), k)
            for s in selected
            for k in wanted[s["id"]]
            + ([summary_key(wanted[s["id"]][0])] if kind in SUMMARY_KINDS else [])
            + (["attention:" + kind] if focused and s.get("platform") == "pdd" else [])
        ]
        patterns = [("tax_record:", [(s["node_id"], str(s["local_id"])) for s in selected])] if kind == "tax" else []
        if focused:
            patterns.append(
                (
                    "resolved:" + kind + ":",
                    [(s["node_id"], str(s["local_id"])) for s in selected if s.get("platform") == "pdd"],
                )
            )
        loaded = load(self.database, exact, patterns)
        if kind == "orders":
            from .order_api import expand

            loaded = expand(self.database, loaded)
        for shop in selected:
            stored = loaded.get((shop["node_id"], str(shop["local_id"])), [])
            keys = wanted[shop["id"]]
            candidates = sorted((r for r in stored if r["scope"] in keys), key=lambda r: r["updated"], reverse=True)
            saved = next(
                (r for r in candidates if kind == "orders" and r["scope"] == keys[0]),
                candidates[0] if candidates else None,
            )
            if kind == "orders" and options.get("status") == "待发货":
                from ..list_completion import order_key, order_candidate

                complete = next(
                    (r for r in candidates if r["scope"] == order_key(shop) and order_candidate(r["payload"], options)),
                    None,
                )
                if complete and (not saved or complete["updated"] >= saved["updated"]):
                    saved = complete
            if kind in SUMMARY_KINDS:
                brief = next((r for r in stored if r["scope"] == summary_key(keys[0])), None)
                saved = merge_summary(kind, saved, brief)
            payload = dict(saved["payload"]) if saved else {}
            updated = saved["updated"] if saved else 0
            if kind == "tax":
                from ..audit_store import differences, comparison

                canonical_rows = []
                for item in (r for r in stored if r["scope"].startswith("tax_record:")):
                    _, period, version = item["scope"].split(":")
                    if (
                        int(version) != shop.get("profile_version", 0)
                        or not options["date_from"][:7] <= period < options["date_to"][:7]
                    ):
                        continue
                    data = item["payload"]
                    row = dict(data["latest"])
                    absent = row.pop("_missing_from_source", False)
                    if (
                        row.get("official_tax")
                        and shop.get("platform") in ("taobao", "jd")
                        and row["period"] != options["period"]
                    ):
                        continue
                    canonical_rows.append(
                        {
                            **row,
                            "db_comparison": {
                                **comparison(
                                    row["period"],
                                    differences(data["baseline"], row),
                                    missing=absent,
                                    row=row,
                                    previous=data["baseline"],
                                ),
                                "baseline_at": data["baseline_at"],
                                "checked_at": data["checked_at"],
                            },
                        }
                    )
                if canonical_rows:
                    payload["rows"] = canonical_rows
            if (
                kind in ("aftersales", "evidence", "tickets", "intercepts", "packages", "invoices")
                and shop.get("platform") == "pdd"
            ):
                from ..focus_records import covered

                focus = next((r for r in stored if r["scope"] == "attention:" + kind), None)
                facts = [r for r in stored if r["scope"].startswith("resolved:" + kind + ":")]
                if focus:
                    fresh = {str(r["id"]): r for r in focus["payload"].get("rows", [])}
                    resolved = {r["scope"].split(":", 2)[2]: r for r in facts}
                    normal_started = payload.get("_sync", {}).get("snapshot_started_at", updated)
                    focus_started = focus["payload"].get("_sync", {}).get("snapshot_started_at", focus["updated"])
                    merged = []
                    for original in payload.get("rows", []):
                        row = dict(original)
                        rid = str(row["id"])
                        if focus_started > normal_started:
                            if rid in fresh:
                                row.update(fresh[rid])
                            elif covered(
                                kind,
                                row,
                                focus["payload"].get("_sync", {}).get("scope"),
                                payload.get("_sync", {}).get("scope"),
                            ):
                                row["attention_excluded"] = True
                                row.pop("attention_status", None)
                        fact = resolved.get(rid)
                        if fact and fact["updated"] >= max(normal_started, focus_started):
                            row.update(fact["payload"])
                        merged.append(row)
                    payload["rows"] = merged
            if not saved:
                missing.append(shop)
            freshness = describe(
                kind,
                payload.get("_sync", {}).get("scope", options),
                payload,
                updated,
                policies.get(shop["node_id"]),
                shop.get("platform", "pdd"),
            )
            source = {
                **identity(shop),
                "shop_id": shop["id"],
                "shop_name": shop.get("name", ""),
                "operators": shop.get("operators", []),
                "state": shop.get("state", "stopped"),
                "collector_online": shop["collector_online"],
                **freshness,
                "updated_at": datetime.fromtimestamp(updated, BEIJING).isoformat() if updated else None,
                "cache_missing": not bool(saved),
                "center_received_at": datetime.fromtimestamp(payload["_center_received_at"], BEIJING).isoformat()
                if payload.get("_center_received_at")
                else None,
                "complete": payload.get("complete", False),
                "loaded_count": len(payload.get("rows", [])),
                "source_total": payload.get("source_total"),
                "source_name": payload.get("source_name"),
                "scope_note": payload.get("scope_note"),
                "stale": not saved
                or freshness["overdue"]
                or not shop["collector_online"]
                or shop.get("state") not in ("logged_in", "sleeping"),
                "syncing": not bool(saved) and shop.get("state") in ("logged_in", "sleeping"),
                "error": (
                    "店铺未登录，显示中心已存数据" if shop.get("state") not in ("logged_in", "sleeping") else None
                )
                if shop["collector_online"]
                else "采集节点离线，显示中心已存数据",
                "data_status": "ready" if saved else "missing",
            }
            if payload.get("entity"):
                source["entity"] = payload["entity"]
            source.update(
                summary_complete=bool(payload.get("summary_complete")),
                details_pending=bool(payload.get("details_pending")),
                details_on_demand=bool(payload.get("details_on_demand")),
                details_complete=bool(payload.get("details_complete", payload.get("complete"))),
                summary_updated_at=payload.get("summary_updated_at"),
                details_updated_at=payload.get(
                    "details_updated_at", updated if saved and not payload.get("details_pending") else None
                ),
            )
            if read_only:
                from ..home_counts import source_fields

                source.update(source_fields(payload))
            if "_creation_counts" in payload:
                source["_creation_counts"] = payload["_creation_counts"]
            sources.append(source)
            metric = payload.get("metrics")
            if metric:
                metric = dict(metric)
                if (
                    kind == "overview"
                    and datetime.fromtimestamp(
                        payload.get("_sync", {}).get("snapshot_started_at") or updated, BEIJING
                    ).date()
                    != datetime.now(BEIJING).date()
                ):
                    for name in ("sales", "orders", "ad_spend", "visitors", "views", "reviews_today"):
                        metric[name] = None
                    source["day_pending"] = True
                metrics.append({**metric, **source})
            rows.extend(
                {
                    **decorate(kind, r, shop.get("platform", "pdd")),
                    **identity(shop),
                    "shop_id": shop["id"],
                    "shop_name": shop.get("name", ""),
                    "operator": shop.get("operator", ""),
                    "operators": shop.get("operators", []),
                }
                for r in payload.get("rows", [])
            )
        if missing and not read_only:
            self.request_sync(kind, options, shops=missing, source="automatic")
        totals = {}
        if kind == "overview":
            for key in (
                "sales",
                "orders",
                "ad_spend",
                "visitors",
                "views",
                "reviews_today",
                "pending_payment",
                "pending_shipping",
                "pending_aftersales",
                "pending_tickets",
                "shipping_due",
                "aftersales_due",
            ):
                numbers = [r[key] for r in metrics if isinstance(r.get(key), (float, int))]
                totals[key] = round(sum(numbers), 2) if numbers else None
        result = {"sources": sources, "rows": rows, "metrics": metrics, "totals": totals, "timezone": "Asia/Shanghai"}
        if kind == "overview":
            result["previous"] = self.previous(metrics)
        return result

    def previous(self, current):
        from ..home_comparison import FIELDS, valid
        from .. import transactions

        day = (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
        previous = {}
        by_id = {s["id"]: s for s in self.store.list()}
        scopes = {}
        for row in current:
            shop = by_id[row["shop_id"]]
            key = self.key("transactions", transactions.options_for("yesterday"), shop)
            scopes[row["shop_id"]] = (f"history:overview:{day}:{shop.get('profile_version', 0)}", key, "archive:" + key)
        loaded = load(
            self.database,
            [
                (by_id[i]["node_id"], str(by_id[i]["local_id"]), scope)
                for i, values in scopes.items()
                for scope in values
            ],
        )
        for row in current:
            shop = by_id[row["shop_id"]]
            historical = scopes[row["shop_id"]][0]
            records = sorted(
                (
                    r
                    for r in loaded.get((shop["node_id"], str(shop["local_id"])), [])
                    if r["scope"] in scopes[row["shop_id"]]
                ),
                key=lambda r: r["updated"],
            )
            old = {}
            for saved in records:
                payload = saved["payload"]
                if not payload.get("complete"):
                    continue
                if saved["scope"] == historical:
                    old.update(payload.get("metrics", {}))
                else:
                    summary = payload.get("metrics", {}).get("summary", {})
                    for name in ("sales", "orders"):
                        if valid(summary.get(name)):
                            old[name] = summary[name]
            previous[row["shop_id"]] = old
        fields = {}
        for key in FIELDS:
            known = [v[key] for v in previous.values() if valid(v.get(key))]
            pairs = [
                (r[key], previous[r["shop_id"]][key])
                for r in current
                if valid(r.get(key)) and valid(previous[r["shop_id"]].get(key))
            ]
            today = sum(a for a, b in pairs)
            yesterday = sum(b for a, b in pairs)
            fields[key] = {
                "value": round(sum(known), 2) if known else None,
                "known": len(known),
                "total": len(current),
                "comparable": len(pairs),
                "label": "昨日参考",
                "basis": "中心已存交易报表与日快照",
                "change": round((today - yesterday) / yesterday * 100, 2) if pairs and yesterday else None,
                "direction": "up"
                if today > yesterday
                else "down"
                if today < yesterday
                else "flat"
                if pairs
                else "unknown",
                "today_comparable": today if pairs else None,
                "yesterday_comparable": yesterday if pairs else None,
            }
        return {"date": day, "fields": fields, "basis": "按双方都有数据的相同店铺比较"}
