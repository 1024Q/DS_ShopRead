"""Display identity, kept separate from the original name and stable shop ID."""

from .platforms import PLATFORMS


def shop_label(record):
    if record.get("shop_label"):
        return record["shop_label"]
    name = record.get("shop_name") or record.get("name") or f"店铺 {record.get('shop_id', record.get('id', '未知'))}"
    return f"{PLATFORMS.get(record.get('platform') or record.get('shop_platform'), '平台未知')} · {name}"


def identity(record):
    return {
        "operators": record.get("operators") or ([record["operator"]] if record.get("operator") else []),
        "platform": record.get("platform", "pdd"),
        "shop_label": shop_label({**record, "platform": record.get("platform", "pdd")}),
    }
