"""Bounded synchronization details; full retry scopes are fetched on demand."""

from .filter_scope import matches
from .table_sort import sort_rows


def source_page(
    data,
    shop_id="",
    kind="",
    cadence="",
    state="",
    keyword="",
    sort_by="",
    direction="asc",
    page=1,
    size=30,
    retry=False,
    details=False,
    snapshot=False,
):
    sources = data.get("sources", [])

    def issue(s):
        return s.get("state") != "stopped" and bool(
            s.get("error")
            or s.get("waiting_login")
            or s.get("failed_scopes")
            or s.get("failed_scope_count")
            or s.get("data_status") in ("error", "blocked")
        )

    def active(s):
        return bool(s.get("syncing") or s.get("data_status") in ("queued", "running"))

    def selected(s):
        return (
            matches(shop_id, s["shop_id"])
            and (not kind or s["kind"] == kind)
            and (
                not cadence
                or (
                    cadence
                    in s.get(
                        "queues", [s.get("queue") or ("focus" if s.get("scope", {}).get("_attention") else "normal")]
                    )
                    if cadence != "off"
                    else s.get("cadence") == cadence
                )
            )
            and (
                not state
                or (
                    issue(s)
                    if state == "issues"
                    else active(s)
                    if state == "active"
                    else s.get("updated_at")
                    if state == "recorded"
                    else s.get("data_status") == state
                )
            )
            and (not keyword or keyword.casefold() in str(s).casefold())
        )

    filtered = [s for s in sources if selected(s)]
    filtered.sort(
        key=lambda s: (
            0 if issue(s) else 1 if active(s) else 2 if s.get("data_status") == "partial" else 3,
            s["shop_id"],
            s["kind"],
        )
    )
    if sort_by:
        filtered = sort_rows(filtered, sort_by, direction)
    eligible = [
        s
        for s in filtered
        if issue(s)
        and (s.get("failed_scopes") or not active(s))
        and not s.get("waiting_login")
        and s.get("state") != "stopped"
    ]
    pages = max(1, (len(filtered) + size - 1) // size)
    page = min(page, pages)
    rows = eligible if retry else filtered if snapshot else filtered[(page - 1) * size : page * size]
    if not retry and not details:
        rows = [
            {
                **{k: v for k, v in row.items() if k != "failed_scopes"},
                "failed_scope_count": row.get("failed_scope_count", len(row.get("failed_scopes") or [])),
            }
            for row in rows
        ]
    return {
        "snapshot_complete": bool(snapshot),
        "rows": rows,
        "page": page,
        "page_count": pages,
        "total": len(filtered),
        "retry_count": sum(len(s.get("failed_scopes") or [s]) for s in eligible),
        "waiting": sum(bool(s.get("waiting_login")) for s in filtered),
        "kinds": sorted({s["kind"] for s in sources}),
        "counts": {
            "all": len(sources),
            "issues": sum(issue(s) for s in sources),
            "active": sum(active(s) for s in sources),
            "ready": sum(bool(s.get("updated_at")) for s in sources),
        },
    }
