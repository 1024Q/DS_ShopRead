"""Multiple operators per shop, with a legacy display label for older clients."""

import re


def names(value):
    values = re.split(r"[,，、;；\r\n]+", value) if isinstance(value, str) else value
    result = []
    for item in values or []:
        if not isinstance(item, str):
            raise ValueError("运营人员姓名格式不正确")
        item = item.strip()
        if not item:
            continue
        if len(item) > 100 or any(c in item for c in "\r\n\0"):
            raise ValueError("运营人员姓名格式不正确")
        if item.casefold() not in {n.casefold() for n in result}:
            result.append(item)
    if len(result) > 100:
        raise ValueError("一次最多添加或分配 100 位运营人员")
    return result


def initialize(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS shop_operators (shop_id INTEGER NOT NULL,name TEXT NOT NULL COLLATE NOCASE,PRIMARY KEY(shop_id,name))"
    )
    if not db.execute("SELECT 1 FROM settings WHERE key='multi_operators_v1'").fetchone():
        for row in db.execute("SELECT id,operator FROM shops WHERE operator<>''").fetchall():
            replace(db, row["id"], names(row["operator"]))
        db.execute("INSERT INTO settings(key,value) VALUES('multi_operators_v1','true')")


def replace(db, shop_id, people):
    db.execute("DELETE FROM shop_operators WHERE shop_id=?", (shop_id,))
    db.executemany("INSERT OR IGNORE INTO operators(name) VALUES(?)", [(n,) for n in people])
    db.executemany("INSERT OR IGNORE INTO shop_operators(shop_id,name) VALUES(?,?)", [(shop_id, n) for n in people])
    db.execute("UPDATE shops SET operator=? WHERE id=?", (people[0] if people else "", shop_id))


def attach(db, shops):
    assigned = {}
    for row in db.execute("SELECT shop_id,name FROM shop_operators ORDER BY name"):
        assigned.setdefault(row["shop_id"], []).append(row["name"])
    return [
        {**s, "operators": assigned.get(s["id"], []), "operator": "、".join(assigned.get(s["id"], []))} for s in shops
    ]


def publish(db, ids):
    from .data_versions import publish

    for sid in ids:
        publish(db, sid, "*")
