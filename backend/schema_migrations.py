"""Additive SQLite migrations; legacy code safely ignores the version ledger."""

import time


def apply(db, scope, steps):
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (scope TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, applied_at REAL NOT NULL, PRIMARY KEY(scope,version))"
    )
    known = {row[0] for row in db.execute("SELECT version FROM schema_migrations WHERE scope=?", (scope,))}
    for version, name, migrate in steps:
        if version in known:
            continue
        # A savepoint also makes standalone callers atomic, including DDL.
        db.execute("SAVEPOINT schema_step")
        try:
            migrate(db)
            db.execute("INSERT INTO schema_migrations VALUES(?,?,?,?)", (scope, version, name, time.time()))
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if version > current:
                db.execute(f"PRAGMA user_version={int(version)}")
            db.execute("RELEASE schema_step")
        except BaseException:
            db.execute("ROLLBACK TO schema_step")
            db.execute("RELEASE schema_step")
            raise


def node_shop_columns(db):
    from .platforms import migrate

    columns = {row[1] for row in db.execute("PRAGMA table_info(shops)")}
    for name, definition in (
        ("alias", "TEXT NOT NULL DEFAULT ''"),
        ("profile_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE shops ADD COLUMN {name} {definition}")
    migrate(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(shops)")}
    for name, definition in (
        ("operator", "TEXT NOT NULL DEFAULT ''"),
        ("login_method", "TEXT NOT NULL DEFAULT 'password'"),
        ("login_url", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE shops ADD COLUMN {name} {definition}")


def node(db):
    apply(db, "node_management", [(1, "existing_shop_columns", node_shop_columns)])


def shop(db):
    def verify(connection):
        required = {"business_cache", "refund_case_index", "settings"}
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if required - names:
            raise RuntimeError("店铺库结构尚未完成初始化，未记录迁移版本")

    apply(db, "shop_business", [(1, "verified_sharded_baseline", verify)])
