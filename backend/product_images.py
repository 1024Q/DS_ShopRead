"""Shared product image contract; never use review photos or proof attachments."""

from urllib.parse import urlsplit

KINDS = {
    "orders",
    "ad_products",
    "ad_reports",
    "products",
    "reviews",
    "tickets",
    "packages",
    "invoices",
    "aftersales",
    "evidence",
    "intercepts",
    "navigator",
    "quality",
    "brands",
    "goodsdata",
    "activities",
}
FIELDS = ("thumbnail_url", "hdThumbUrl", "hd_thumb_url", "thumbUrl", "thumb_url", "imgUrl", "imageUrl", "image_url")


def thumbnail(item):
    for key in FIELDS:
        value = item.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value.startswith("//"):
            value = "https:" + value
        try:
            parts = urlsplit(value)
            if parts.scheme in ("https", "http") and parts.hostname and not parts.username and not parts.password:
                return value
        except ValueError:
            pass
    return ""


def enrich(rows, catalog):
    result = []
    for row in rows:
        known = catalog.get(str(row.get("goods_id") or ""), {})
        result.append(
            {
                **row,
                "thumbnail_url": thumbnail(row) or known.get("thumbnail_url", ""),
                "name": row.get("name") or known.get("name", ""),
            }
        )
    return result
