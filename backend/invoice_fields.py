"""Preserve observed PDD invoice application details; never infer missing values."""

import re

EXPORT_FIELDS = (
    "shop_name",
    "company_name",
    "operator",
    "order_number",
    "id",
    "status",
    "created_at",
    "deadline",
    "amount",
    "invoice_title_type",
    "invoice_title",
    "buyer_taxpayer_number",
    "buyer_bank",
    "buyer_bank_account",
    "buyer_address",
    "buyer_phone",
    "name",
    "goods_spec",
    "quantity",
    "sku_code",
    "invoice_number",
    "invoice_code",
    "invoiced_at",
    "uploaded_at",
    "paper_receiver_name",
    "paper_receiver_mobile",
    "paper_receiver_address",
    "paper_tracking_number",
    "invoice_other_info",
)


def backfill(store, shop_id, payload):
    """Enrich matching applications without claiming historical statuses were refreshed."""
    import json
    from .table_index import write

    if not payload.get("complete"):
        raise ValueError("未开票列表尚未采集完整")
    fields = set(details({}))
    incoming = {str(r["id"]): r for r in payload.get("rows", []) if r.get("invoice_details_version") == 2}
    with store.connect() as db:
        snapshots = db.execute(
            "SELECT cache_key,payload,updated FROM business_cache WHERE shop_id=? AND cache_key LIKE 'invoices:%'",
            (shop_id,),
        ).fetchall()
        for snapshot in snapshots:
            saved = json.loads(snapshot["payload"])
            changed = False
            for row in saved.get("rows", []):
                fresh = incoming.get(str(row.get("id")))
                if not fresh or row.get("order_number") != fresh.get("order_number"):
                    continue
                additions = {key: fresh.get(key) for key in fields}
                if any(row.get(k) != v or k not in row for k, v in additions.items()):
                    row.update(additions)
                    changed = True
            if changed:
                db.execute(
                    "UPDATE business_cache SET payload=? WHERE shop_id=? AND cache_key=?",
                    (json.dumps(saved, ensure_ascii=False), shop_id, snapshot["cache_key"]),
                )
                write(db, shop_id, snapshot["cache_key"], saved, snapshot["updated"])
        store.invalidate_responses(shop_id, "invoices", db=db)


def details(item):
    from .business_reader import date_text

    info = str(item.get("other_info") or "")
    result = {
        "invoice_details_version": 2,
        # Verified against PDD invoice form business_type enum (individual=0, enterprise=1).
        "invoice_title_type": {0: "个人", 1: "企业", "0": "个人", "1": "企业"}.get(item.get("business_type"), "")
        if not isinstance(item.get("business_type"), bool)
        else "",
        "invoice_title_type_code": item.get("business_type"),
        "buyer_taxpayer_number": str(item.get("payer_register_no") or ""),
        "goods_spec": item.get("spec") or "",
        "quantity": item.get("goods_number"),
        "sku_code": item.get("out_sku_sn") or "",
        "invoice_other_info": info,
        "invoice_code": item.get("invoice_code") or "",
        "invoiced_at": date_text(item.get("invoice_time")) if item.get("invoice_time") else "",
        "uploaded_at": date_text(item.get("upload_time")) if item.get("upload_time") else "",
        "paper_receiver_name": item.get("paper_receiver_name") or "",
        "paper_receiver_mobile": item.get("paper_receiver_mobile") or "",
        "paper_receiver_address": item.get("paper_receiver_address") or "",
        "paper_tracking_number": item.get("paper_tracking_number") or "",
    }
    # Keep account/phone values as text, including leading zeros. An unrecognized
    # or ambiguous label stays in the original field for manual inspection.
    labels = {
        "开户银行": "buyer_bank",
        "银行账号": "buyer_bank_account",
        "账号": "buyer_bank_account",
        "地址": "buyer_address",
        "电话": "buyer_phone",
    }
    found = {}
    for piece in re.split(r"[;；\r\n]+", info):
        match = re.fullmatch(r"\s*([^:：]+)[:：]\s*(.*?)\s*", piece)
        if match and match[1].strip() in labels:
            key = labels[match[1].strip()]
            found.setdefault(key, set()).add(match[2])
    for key in set(labels.values()):
        values = found.get(key, set())
        result[key] = next(iter(values)) if len(values) == 1 else ""
    return result
