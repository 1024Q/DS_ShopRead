"""Select one representative scope per shop, business module and frequency queue."""

import json
from .collection_queue import queue_for


def summary_metadata(cached):
    """Project a verified summary onto its task without relabeling the detail clock."""
    from .summary_publication import KINDS

    result = {}
    briefs = []
    for row in cached:
        key = row["shop_id"], row["cache_key"]
        if row["cache_key"].startswith("summary:"):
            briefs.append(row)
        elif key not in result or row["updated"] > result[key]["updated"]:
            result[key] = row
    for brief in briefs:
        original = brief["cache_key"][8:]
        if original.split(":")[0] not in KINDS or not brief.get("summary_complete"):
            continue
        key = brief["shop_id"], original
        full = result.get(key, {})
        if full.get("updated", 0) >= brief["updated"]:
            continue
        merged = {
            **full,
            **{k: brief.get(k) for k in ("shop_id", "updated", "scope", "source_time", "duration_seconds")},
            "cache_key": original,
            "summary_complete": True,
            "details_pending": True,
            "details_on_demand": bool(brief.get("details_on_demand")),
            "details_complete": bool(full.get("details_complete", full.get("complete"))),
            "summary_updated_at": brief["updated"],
            "details_updated_at": full.get("details_updated_at", full.get("updated")),
            "complete": False,
            "delivery_key": brief["cache_key"],
            "delivery_origin": brief.get("origin", "business_cache"),
            "collection_mode": "summary",
        }
        # Transaction rows are the same newly verified trend used by the home
        # chart. Promotion product rows remain from the last full detail read.
        if original.startswith("transactions:"):
            merged.update({k: brief.get(k) for k in ("loaded_count", "source_total", "source_unit")})
        result[key] = merged
    return list(result.values())


def source_groups(cached, ledger, checkpoints, progress, errors, deferred, config, platforms):
    def group_for(key):
        task = ledger.get(key, {})
        meta = progress.get(key) or deferred.get(key) or {}
        scope = meta.get("scope") or json.loads(task.get("options") or cached.get(key, {}).get("scope") or "{}")
        kind = meta.get("kind") or task.get("kind") or key[1].split(":")[0]
        return (
            key[0],
            kind,
            queue_for({**meta, "kind": kind, "scope": scope, "platform": platforms.get(key[0], "pdd")}, config),
        )

    latest = {}
    ranks = {}
    for key in set(cached) | set(ledger) | set(progress) | set(deferred) | set(errors):
        task = ledger.get(key, {})
        if task.get("schedule_state") == "archived" and key not in progress:
            continue
        meta = progress.get(key, {})
        stamp = cached.get(key, {}).get("updated", 0)
        rank = (0, stamp or task.get("last_requested", 0), key[1])
        if key in deferred:
            rank = (1, task.get("last_requested", 0), key[1])
        if task.get("continuation"):
            rank = (2, checkpoints.get(key, {}).get("updated", 0), key[1])
        failure = errors.get(key, (task.get("retry_at", 0), task.get("last_error")))
        if failure[1] and failure[0] > stamp:
            rank = (3, failure[0], key[1])
        if meta.get("phase") in ("running", "queued"):
            rank = (5 if meta["phase"] == "running" else 4, meta.get("queued_at", 0), key[1])
        group = group_for(key)
        if group not in ranks or rank > ranks[group]:
            latest[group] = key
            ranks[group] = rank
    return latest, group_for
