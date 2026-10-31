"""One demand registry; conservative range coverage and bounded producer waits."""

import json
from . import collection, collection_plan

DATED = {"reviews", "tickets", "packages", "evidence", "intercepts", "aftersales", "invoices"}
IGNORED = {
    "start",
    "end",
    "date_from",
    "date_to",
    "_relative_days",
    "status",
    "stars",
    "reply",
    "explicit_dates",
    "_exact_dates",
    "_filtered_read",
    "_attention",
    "_attention_rules",
}


def covers(kind, broad, narrow, platform="pdd"):
    # A covering time window or a broader status filter is not an identical
    # platform query. Do not turn that projection into a fresh task success.
    def query(scope):
        result = dict(scope)
        if result.get('_attention') and kind in ('evidence', 'intercepts'):
            result['status'] = '待举证' if kind == 'evidence' else '拦截失败'
            result.pop('_attention', None)
            result.pop('_attention_rules', None)
        for key in ('date_from', 'date_to'):
            result.pop(key, None)
        return result
    if query(broad) != query(narrow):
        return False
    if platform != "pdd" or kind not in DATED or broad.get("_attention"):
        return False
    if broad.get("_invoice_details_refresh") or narrow.get("_invoice_details_refresh"):
        return False
    # These native filters do not map one-to-one to normalized row titles.
    # Share identical query semantics only; never infer a quick-tab result.
    if kind == "aftersales" and (
        broad.get("status", "") != narrow.get("status", "")
        or broad.get("explicit_dates", False) != narrow.get("explicit_dates", False)
        or (broad.get("start"), broad.get("end")) != (narrow.get("start"), narrow.get("end"))
    ):
        return False
    if kind == "invoices" and (
        bool(broad.get("_exact_dates")) != bool(narrow.get("_exact_dates"))
        or bool(broad.get("explicit_dates")) != bool(narrow.get("explicit_dates"))
    ):
        return False
    if (
        kind == "invoices"
        and not broad.get("_exact_dates")
        and not broad.get("explicit_dates")
        and (broad.get("start"), broad.get("end")) != (narrow.get("start"), narrow.get("end"))
    ):
        return False
    if narrow.get("_attention") and kind not in ("evidence", "intercepts"):
        return False
    if kind == "tickets" and narrow.get("status") in ("2小时内将逾期", "违规已处理"):
        return False
    if {k: v for k, v in broad.items() if k not in IGNORED} != {k: v for k, v in narrow.items() if k not in IGNORED}:
        return False
    if not (
        broad.get("start", float("inf")) <= narrow.get("start", -1)
        and broad.get("end", -1) >= narrow.get("end", float("inf"))
    ):
        return False
    wanted = ("待举证" if kind == "evidence" else "拦截失败") if narrow.get("_attention") else narrow.get("status", "")
    if broad.get("status") and broad["status"] != wanted:
        return False
    if broad.get("reply") and broad["reply"] != narrow.get("reply"):
        return False
    if broad.get("stars") and (not narrow.get("stars") or not set(narrow["stars"]).issubset(broad["stars"])):
        return False
    return True


def project(kind, broad, narrow, payload):
    if not payload.get("complete") or not covers(kind, broad, narrow):
        return None
    from .incremental_collection import created
    from .business import filter_rows

    rows = payload.get("rows", [])
    if (broad["start"], broad["end"]) != (narrow["start"], narrow["end"]):
        if any(created(r) is None for r in rows):
            return None
        rows = [r for r in rows if narrow["start"] <= created(r) <= narrow["end"]]
    rows = filter_rows(
        rows,
        kind,
        status="" if kind == "aftersales" else narrow.get("status", ""),
        stars=narrow.get("stars", ()),
        reply=narrow.get("reply", ""),
    )
    if narrow.get("_attention"):
        from .attention import wanted

        rows = [r for r in rows if wanted(kind, r, "pdd")]
    total = len({r.get("order_number", r["id"]) for r in rows}) if kind == "intercepts" else len(rows)
    return {
        **payload,
        "rows": rows,
        "source_total": total,
        "source_loaded": total,
        "_sync": {**payload.get("_sync", {}), "scope": narrow, "collection_mode": "shared"},
    }


class Demands:
    def __init__(self, service):
        self.service = service
        self.shops = {}
        self.waits = {}
        self.merged = {}

    def replace(self, shop, entries):
        merged = {}
        for kind, options, source in entries:
            options = collection_plan.canonical(kind, options, shop.get("platform", "pdd"))
            key = self.service.key(kind, options, shop)
            entry = merged.setdefault(key, {"kind": kind, "scope": options, "sources": set()})
            entry["sources"].add(source)
        if self.shops.get(shop["id"]) == merged:
            return [(m["kind"], m["scope"]) for m in merged.values()]
        self.shops[shop["id"]] = merged
        with self.service.store.connect() as db:
            db.execute("DELETE FROM collection_demands WHERE shop_id=?", (shop["id"],))
            db.executemany(
                "INSERT INTO collection_demands(shop_id,cache_key,kind,options,sources) VALUES(?,?,?,?,?)",
                [
                    (shop["id"], key, m["kind"], json.dumps(m["scope"]), json.dumps(sorted(m["sources"])))
                    for key, m in merged.items()
                ],
            )
        return [(m["kind"], m["scope"]) for m in merged.values()]

    def coalesce(self, candidates, now):
        # All producers share the canonical task identity. Keep the oldest wait
        # and the union of unstarted ranges, never erase a continuation's scope.
        from .demand_refresh import refresh

        unique = {}
        for c in candidates:
            shop, item, kind, scope, state = c
            identity = (shop["id"], self.service.key(kind, scope, shop))
            previous = unique.get(identity)
            if previous:
                old = previous[4]
                meta = {"kind": kind, "phase": "queued", "scope": previous[3], **old}
                refresh(meta, scope, now, self.service.queue_config)
                state = {
                    **state,
                    **old,
                    "waiting_since": min(old.get("waiting_since", now), state.get("waiting_since", now)),
                }
                c = (shop, item, kind, meta["scope"], state)
            unique[identity] = c
        candidates = list(unique.values())
        result = []
        merged = {}
        seen = set()
        groups = {}
        for c in candidates:
            groups.setdefault((c[0]["id"], c[2]), []).append((c, (c[0]["id"], self.service.key(c[2], c[3], c[0]))))
        for c in candidates:
            shop, item, kind, scope, state = c
            key = (shop["id"], self.service.key(kind, scope, shop))
            if key in seen:
                continue
            seen.add(key)
            if (
                scope.get("_attention")
                or state.get("resolution_due")
                or state.get("urgent_refund")
                or state.get("continuation")
                or state.get("failures")
            ):
                result.append(c)
                continue
            producers = []
            for other, otherkey in groups[(shop["id"], kind)]:
                s, _, k, o, t = other
                if (
                    s["id"] != shop["id"]
                    or k != kind
                    or otherkey == key
                    or t.get("failures")
                    or t.get("resolution_due")
                ):
                    continue
                if covers(kind, o, scope, shop.get("platform", "pdd")) and (
                    not covers(kind, scope, o, shop.get("platform", "pdd")) or otherkey < key
                ):
                    producers.append((otherkey, o))
            for otherkey, m in self.service.progress.items():
                unavailable = (
                    m.get("blocked")
                    or m.get("retry_at", 0) > now
                    or m.get("database_pressure")
                    or m.get("browser_blocked")
                )
                if (
                    otherkey[0] == shop["id"]
                    and otherkey != key
                    and m["kind"] == kind
                    and not m.get("force")
                    and not unavailable
                    and covers(kind, m.get("scope", {}), scope, shop.get("platform", "pdd"))
                ):
                    producers.append((otherkey, m["scope"]))
            started = self.waits.setdefault(key, now) if producers else now
            # A slow/failed producer cannot indefinitely suppress the smaller demand.
            running = any(self.service.progress.get(p[0], {}).get("phase") == "running" for p in producers)
            if producers and now - started < (240 if running else 60):
                merged[key] = {
                    "kind": kind,
                    "scope": scope,
                    "producer": producers[0][0][1],
                    "stage": "等待结果复用",
                    "waiting_since": started,
                }
            else:
                result.append(c)
        self.waits = {k: v for k, v in self.waits.items() if k in seen}
        self.merged = merged
        return result

    def publish(self, shop, kind, scope, key, payload):
        if not payload.get("complete") or shop.get("platform", "pdd") != "pdd":
            return
        from . import attention

        for target, m in list(self.shops.get(shop["id"], {}).items()):
            if target == key or m["kind"] != kind:
                continue
            progress = self.service.progress.get((shop["id"], target), {})
            if progress.get("phase") == "running" or progress.get("force") or progress.get("urgent_due"):
                continue
            data = project(kind, scope, m["scope"], payload)
            if data is None:
                continue
            existing = collection.state(self.service.store, shop["id"], target)
            started = payload.get("_sync", {}).get("snapshot_started_at", 0)
            if not started or existing and existing.get("last_success", 0) > started:
                continue
            data["_sync"]["shared_from"] = key
            collection.register(self.service.store, shop["id"], target, kind, m["scope"], requested=False)
            if m["scope"].get("_attention"):
                attention.publish(self.service.store, shop["id"], kind, data)
            else:
                self.service.store.write_cache(shop["id"], target, data)
                attention.reconcile(self.service.store, shop["id"], kind, data)
            collection.record_change(
                self.service.store,
                shop["id"],
                target,
                kind,
                m["scope"],
                data,
                self.service.queue_config,
                shop.get("platform", "pdd"),
            )
            collection.finish(self.service.store, shop["id"], target)
            from .collection_checkpoint import clear

            clear(self.service.store, shop["id"], target)
            # Dispatcher retires the already-satisfied waiter without assigning
            # it a browser slot. A manual refresh still wins over shared data.
            progress["shared_result_ready"] = True
