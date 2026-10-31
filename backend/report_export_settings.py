"""Fleet-wide manual report exports; retain legacy file retention settings."""

import json


def read(directory):
    value = {}
    for name in ("report-export.json", "report-export.gray.json"):
        try:
            value = json.loads((directory / name).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                value = {}
            break
        except OSError, ValueError:
            continue
    try:
        days = max(1, min(30, int(value.get("retention_days", 7))))
    except TypeError, ValueError:
        days = 7
    # Old gray flags and node pins no longer limit the rollout.
    return {"enabled": True, "retention_days": days}
