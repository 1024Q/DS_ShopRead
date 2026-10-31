"""PDD package center defines pickup timeout as 24 hours after shipping."""

from datetime import datetime, timedelta, timezone
import time

BEIJING = timezone(timedelta(hours=8))


def fields(row, platform=None, *, remaining=False, now=None):
    due = None
    if (platform or row.get("platform") or "pdd") == "pdd" and row.get("status") == "发货未揽件":
        try:
            shipped = datetime.fromisoformat(row.get("created_at") or "")
            if shipped.tzinfo is None:
                shipped = shipped.replace(tzinfo=BEIJING)
            due = shipped + timedelta(hours=24)
        except ValueError, TypeError:
            pass
    result = {"pickup_deadline": due.isoformat() if due else None}
    if remaining:
        result["pickup_hours_remaining"] = (
            round((due.timestamp() - (time.time() if now is None else now)) / 3600, 2) if due else None
        )
    return result
