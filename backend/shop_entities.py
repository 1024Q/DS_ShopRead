"""Latest valid company identity per shop, with an indexed metadata lookup."""


def company_names(store):
    if hasattr(store, "company_names"):
        return store.company_names()
    with store.connect() as db:
        rows = db.execute("""SELECT s.id AS shop_id,(SELECT json_extract(t.latest,'$.entity.company_name')
          FROM tax_records t WHERE t.shop_id=s.id AND t.profile_version=s.profile_version
          AND coalesce(json_extract(t.latest,'$.entity.company_name'),'')<>''
          ORDER BY t.checked_at DESC,t.period DESC LIMIT 1) AS name FROM shops s""").fetchall()
    return {r["shop_id"]: r["name"] for r in rows if r["name"]}
