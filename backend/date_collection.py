"""Explicit date requests only for adapters whose date fields are verified."""

import json
from pathlib import Path
from datetime import datetime
from .business_reader import BEIJING

CAPABILITIES = json.loads(
    (Path(__file__).resolve().parents[1] / "config/date-collection.json").read_text(encoding="utf8")
)


def exact_options(kind, options, date_from, date_to):
    capability = CAPABILITIES.get(kind, {})
    if capability.get("mode") not in ("dates", "catalog"):
        raise ValueError("该模块不支持指定日期采集，请使用已提供的统计范围或账期")
    if not date_from or not date_to:
        raise ValueError("请同时选择采集开始日期和结束日期")
    first, last = datetime.fromisoformat(date_from).date(), datetime.fromisoformat(date_to).date()
    if first > last:
        raise ValueError("开始日期不能晚于结束日期")
    if last > datetime.now(BEIJING).date():
        raise ValueError("采集日期不能晚于北京时间今天")
    if capability["mode"] == "catalog":
        return options
    if (last - first).days >= 180:
        raise ValueError("单次采集最多 180 天，请分段采集")
    return {**options, "_exact_dates": True, "explicit_dates": True}
