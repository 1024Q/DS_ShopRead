"""Reusable successful refund case facts; never substitute an order's paid amount."""

import time, math


def initialize(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS refund_case_index(shop_id INTEGER,case_id TEXT,order_number TEXT,amount REAL,updated REAL,PRIMARY KEY(shop_id,case_id))"
    )


def save(db, shop_id, rows, updated=None):
    stamp = updated or time.time()
    for row in rows:
        amount = row.get("refund_amount", row.get("amount"))
        if (
            row.get("status") != "退款成功"
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
        ):
            continue
        case = str(row.get("aftersale_id") or row.get("id") or "")
        if case and row.get("order_number"):
            db.execute(
                "INSERT INTO refund_case_index(shop_id,case_id,order_number,amount,updated) VALUES(?,?,?,?,?) ON CONFLICT(shop_id,case_id) DO UPDATE SET order_number=excluded.order_number,amount=excluded.amount,updated=excluded.updated WHERE excluded.updated>=refund_case_index.updated",
                (shop_id, case, str(row["order_number"]), amount, stamp),
            )


def lookup(store, shop_id, orders, now=None):
    result = {}
    stamp = now or time.time()
    with store.connect() as db:
        for offset in range(0, len(orders), 400):
            part = orders[offset : offset + 400]
            ids = [str(r.get("aftersale_id") or "") for r in part]
            if not ids:
                continue
            records = {
                r["case_id"]: r
                for r in db.execute(
                    "SELECT * FROM refund_case_index WHERE shop_id=? AND case_id IN ("
                    + ",".join("?" for _ in ids)
                    + ") AND updated>=?",
                    [shop_id, *ids, stamp - 900],
                )
            }
            for row in part:
                case = records.get(str(row.get("aftersale_id") or ""))
                if case and case["order_number"] == row.get("order_number"):
                    result[case["case_id"]] = {
                        "id": case["case_id"],
                        "order_number": case["order_number"],
                        "amount": case["amount"],
                    }
    return result
