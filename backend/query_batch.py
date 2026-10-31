"""Load requested list metadata and task states in bounded SQLite batches."""

import json
from .partition_reads import partitions, read_connection


def read(store, keys, indexed):
    caches = {}
    tasks = {}
    pairs = list(keys.items())
    for sid in partitions(store, keys):
        scoped = pairs if sid is None else [pair for pair in pairs if pair[0] == sid]
        with read_connection(store, sid) as db:
            for offset in range(0, len(scoped), 200):
                batch = scoped[offset : offset + 200]
                cte = "WITH wanted(shop_id,cache_key) AS (VALUES " + ",".join("(?,?)" for _ in batch) + ") "
                args = [v for pair in batch for v in pair]
                if indexed:
                    query = "SELECT c.shop_id,c.cache_key,c.updated,c.metadata AS payload,c.row_count FROM table_snapshots c JOIN wanted w USING(shop_id,cache_key)"
                else:
                    query = "SELECT c.*,NULL AS row_count FROM business_cache c JOIN wanted w USING(shop_id,cache_key) UNION ALL SELECT a.*,NULL AS row_count FROM daily_archive a JOIN wanted w USING(shop_id,cache_key) WHERE NOT EXISTS(SELECT 1 FROM business_cache b WHERE b.shop_id=a.shop_id AND b.cache_key=a.cache_key)"
                for row in db.execute(cte + query, args):
                    payload = json.loads(row["payload"])
                    if indexed:
                        payload["_row_count"] = row["row_count"]
                    caches[row["shop_id"], row["cache_key"]] = {"updated": row["updated"], "payload": payload}
                for row in db.execute(
                    cte + "SELECT c.* FROM collection_tasks c JOIN wanted w USING(shop_id,cache_key)", args
                ):
                    tasks[row["shop_id"], row["cache_key"]] = dict(row)
    return caches, tasks
