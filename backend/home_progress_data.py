"""Read only the clocks needed by homepage coverage, in bounded shop batches."""

from .partition_reads import partitions, read_connection


def read(store, wanted):
    tasks, caches, attention = {}, {}, {}
    ids = {sid for sid, _ in wanted}
    for sid in partitions(store, ids):
        pairs = [(shop, key) for shop, key in wanted if sid is None or shop == sid]
        with read_connection(store, sid) as db:
            for offset in range(0, len(pairs), 150):
                part = pairs[offset : offset + 150]
                if not part:
                    continue
                cte = "WITH wanted(shop_id,cache_key) AS (VALUES " + ",".join("(?,?)" for _ in part) + ") "
                args = [v for pair in part for v in pair]
                for row in db.execute(
                    cte + "SELECT c.* FROM collection_tasks c JOIN wanted w USING(shop_id,cache_key)", args
                ):
                    tasks[row["shop_id"], row["cache_key"]] = dict(row)
                # Existing table metadata avoids decoding full product lists.
                query = """SELECT c.shop_id,c.cache_key,c.updated,c.metadata AS payload FROM table_snapshots c JOIN wanted w USING(shop_id,cache_key)
                    UNION ALL SELECT c.shop_id,c.cache_key,c.updated,c.payload FROM business_cache c JOIN wanted w USING(shop_id,cache_key)
                    WHERE NOT EXISTS(SELECT 1 FROM table_snapshots s WHERE s.shop_id=c.shop_id AND s.cache_key=c.cache_key)
                    UNION ALL SELECT c.shop_id,c.cache_key,c.updated,c.payload FROM business_cache c JOIN wanted w
                    ON c.shop_id=w.shop_id AND c.cache_key='summary:'||w.cache_key"""
                for row in db.execute(
                    cte
                    + """SELECT shop_id,cache_key,updated,
                    COALESCE(json_extract(payload,'$._sync.snapshot_started_at'),updated) AS started,
                    COALESCE(json_extract(payload,'$.complete'),0) AS complete,
                    json_extract(payload,'$.metrics.refund_complete') AS refund_complete,
                    json_extract(payload,'$.metrics.refund_date') AS refund_date,
                    json_extract(payload,'$.metrics.refund_basis') AS refund_basis,
                    COALESCE(json_extract(payload,'$.summary_complete'),0) AS summary_complete,
                    COALESCE(json_extract(payload,'$.details_on_demand'),0) AS details_on_demand FROM ("""
                    + query
                    + ")",
                    args,
                ):
                    caches[row["shop_id"], row["cache_key"]] = dict(row)
            shops = sorted({shop for shop, _ in pairs})
            if shops:
                sql = (
                    """SELECT shop_id,kind,COALESCE(json_extract(payload,'$._sync.full_snapshot_updated_at'),updated) AS updated,
                    COALESCE(json_extract(payload,'$._sync.snapshot_started_at'),started) AS started,1 AS complete
                    FROM attention_snapshots WHERE shop_id IN ("""
                    + ",".join("?" for _ in shops)
                    + ")"
                )
                for row in db.execute(sql, shops):
                    attention[row["shop_id"], row["kind"]] = dict(row)
    return tasks, caches, attention
