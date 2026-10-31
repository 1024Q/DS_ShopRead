"""Demand expiry is separate from saved business data and retry cooldowns."""

import json
import time
from .collection_plan import obsolete_realtime


def retained(record, options, fixed=False, now=None):
    from .list_completion import valid, pending

    if valid(record["kind"], options):
        return pending({**record, "options": options})
    from .task_catalog import removed_report

    if removed_report(record["kind"], options):
        return False
    now = time.time() if now is None else now
    if record["kind"] == "orders":
        from .order_plan import disabled_scope

        if disabled_scope(options):
            return False
    if obsolete_realtime(record["kind"], options):
        return False
    if fixed:
        return True
    if options.get("_attention"):
        return False  # Only the current focus subscription survives.
    if record["kind"] == "orders" and options.get("_order_lane") in ("history", "daily", "recheck"):
        return bool(record.get("continuation") or record.get("failures") or not record.get("last_success"))
    return record.get("demand_until", 0) > now


def reconcile(store, shop, fixed, key_for, active, now=None):
    """Archive obsolete demands; remove only abandoned checkpoints, never saved rows."""
    now = time.time() if now is None else now
    with store.connect() as db:
        records = [dict(r) for r in db.execute("SELECT * FROM collection_tasks WHERE shop_id=?", (shop["id"],))]
    from .order_plan import key as order_key
    from .table_index import metadata

    saved = metadata(store, shop["id"], order_key(shop)) if any(r["kind"] == "orders" for r in records) else None
    covered_days = saved["payload"].get("_order_days", {}) if saved else {}
    expired = []
    reset = []
    retained_rows = []
    from . import collection
    from .home_only_pause import held

    config = collection.settings(store)
    paused = set()
    for record in records:
        key = record["cache_key"]
        options = json.loads(record["options"])
        from .list_completion import valid as completion_scope

        if completion_scope(record["kind"], options):
            paused.add(key)  # Offline one-shot checkpoints remain resumable.
        if held(record["kind"], options, config, shop.get("platform", "pdd")):
            paused.add(key)
            continue  # A user pause must not erase an unfinished checkpoint.
        valid = key == key_for(record["kind"], options, shop) and retained(record, options, key in fixed, now)
        if valid and record["kind"] == "orders" and options.get("_order_lane") == "history":
            from .order_plan import requested_refresh

            stamp = covered_days.get(options.get("date_from"), 0)
            if stamp and not requested_refresh(record, stamp):
                valid = False
        if key in active:
            if valid:
                retained_rows.append(record)
            continue
        if not valid:
            if record["schedule_state"] != "archived" or record["continuation"]:
                expired.append(key)
        else:
            retained_rows.append(record)
    # A stale partial snapshot must be revalidated from page one, not resumed forever.
    with store.connect() as db:
        reset = [
            r[0]
            for r in db.execute(
                "SELECT cache_key FROM collection_checkpoints WHERE shop_id=? AND updated<?", (shop["id"], now - 86400)
            )
            if r[0] not in active and r[0] not in paused
        ]
    if expired or reset:
        with store.write_connection() as db:
            for key in set(expired + reset):
                db.execute("DELETE FROM collection_checkpoints WHERE shop_id=? AND cache_key=?", (shop["id"], key))
                db.execute(
                    "UPDATE collection_tasks SET continuation=0 WHERE shop_id=? AND cache_key=?", (shop["id"], key)
                )
            for key in expired:
                db.execute(
                    "UPDATE collection_tasks SET schedule_state='archived',lease_until=0 WHERE shop_id=? AND cache_key=?",
                    (shop["id"], key),
                )
    return retained_rows


def before_run(store, shop, kind, scope, key, key_for, force=False, subscribed=False, config=None, urgent=False):
    from .home_only_pause import held
    from . import collection

    config = config or collection.settings(store)
    if held(kind, scope, config, shop.get("platform", "pdd")):
        return False
    with store.connect() as db:
        identity = db.execute("SELECT profile_version,platform FROM shops WHERE id=?", (shop["id"],)).fetchone()
        row = db.execute("SELECT * FROM collection_tasks WHERE shop_id=? AND cache_key=?", (shop["id"], key)).fetchone()
    if (
        not identity
        or identity["profile_version"] != shop.get("profile_version", 0)
        or identity["platform"] != shop.get("platform", "pdd")
    ):
        return False
    if key != key_for(kind, scope, shop) or not row:
        return False
    if not force and kind == "orders":
        from .order_plan import covered

        if covered(store, shop, scope, dict(row)):
            return False
    record = dict(row)
    urgent = bool(urgent and kind == "aftersales" and scope.get("_attention") and shop.get("platform", "pdd") == "pdd")
    if not force and not urgent and record.get("last_success") and not record.get("failures") and not record.get("continuation"):
        # A covering query may have published this queued demand while it waited.
        # Do not spend another browser slot rereading the same fresh result.
        from . import collection

        if not collection.due(
            kind,
            scope,
            record["last_success"],
            config or collection.settings(store),
            platform=shop.get("platform", "pdd"),
            adaptive_seconds=record.get("adaptive_seconds", 0),
        ):
            return False
    return retained(record, scope, subscribed or record["schedule_state"] == "periodic", time.time()) and (
        force or subscribed or record["schedule_state"] != "archived"
    )
