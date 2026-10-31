"""Exercise real PostgreSQL leases in an isolated transaction-backed test schema."""

import json
import secrets
from pathlib import Path
import psycopg
from psycopg import sql
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from backend.center.database import Database, nodes, jobs, snapshots, metadata


def main():
    config = json.loads(Path(".data/center/center.local.json").read_text(encoding="utf-8-sig"))
    url = config["ECMP_DATABASE_URL"]
    parsed = make_url(url)
    namespace = "verify_" + secrets.token_hex(8)
    connection = psycopg.connect(
        host=parsed.host,
        port=parsed.port,
        dbname=parsed.database,
        user=parsed.username,
        password=parsed.password,
        autocommit=True,
    )
    try:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        engine = create_engine(url, connect_args={"options": f"-c search_path={namespace}"})
        try:
            metadata.create_all(engine)
            database = Database.__new__(Database)
            database.engine = engine
            node = database.register("PostgreSQL 验证节点")
            assert database.authenticate(node["id"], node["token"])
            assert not database.authenticate(node["id"], "invalid")
            job = database.enqueue(node["id"], {"kind": "overview", "shop_id": 1})
            assert job == database.enqueue(node["id"], {"kind": "overview", "shop_id": 1})
            claimed = database.claim(node["id"])
            assert database.claim(node["id"]) is None
            with engine.begin() as db:
                db.execute(update(jobs).where(jobs.c.id == job).values(expires=0))
            reclaimed = database.claim(node["id"])
            assert not database.finish(node["id"], job, claimed["lease"], {"ok": True})
            assert database.finish(node["id"], job, reclaimed["lease"], {"ok": True})
            for timestamp in (20, 20, 10):
                database.ingest(
                    node["id"], [{"shop": "1", "scope": "overview", "updated": timestamp, "payload": {"sales": 100}}]
                )
            records = database.list(snapshots)
            assert len(records) == 1 and records[0]["updated"] == 20
            print(
                "PostgreSQL integration passed: auth, deduplication, lease fencing, JSON snapshots, stale-write protection."
            )
        finally:
            engine.dispose()
    finally:
        # Only the randomly generated verification schema is removed; production tables stay untouched.
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(namespace)))
        connection.close()


if __name__ == "__main__":
    main()
