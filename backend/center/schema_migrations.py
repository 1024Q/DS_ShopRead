"""Version the center core schema without dropping or rewriting existing data."""

import time
from sqlalchemy import Column, Integer, String, Float, MetaData, Table, select, text

history = Table(
    "schema_migrations",
    MetaData(),
    Column("scope", String, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("applied_at", Float, nullable=False),
)


def core(engine, metadata):
    with engine.begin() as db:
        if engine.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(1789342011)"))
        history.create(db, checkfirst=True)
        # Retain idempotent creation for tables registered by installed modules.
        metadata.create_all(db)
        present = db.execute(
            select(history.c.version).where(history.c.scope == "center_core", history.c.version == 1)
        ).first()
        if not present:
            db.execute(
                history.insert().values(
                    scope="center_core", version=1, name="existing_core_tables", applied_at=time.time()
                )
            )
