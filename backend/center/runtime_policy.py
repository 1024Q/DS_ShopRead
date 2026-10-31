"""Read only public scheduling policy for the node heartbeat, without opening a Store."""

import json
import sqlite3
from contextlib import closing


def read(directory):
    try:
        with closing(
            sqlite3.connect((directory / "shops.sqlite3").resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as db:
            row = db.execute("SELECT value FROM settings WHERE key='collection_policy_v3'").fetchone()
        return json.loads(row[0]) if row else None
    except sqlite3.Error, OSError, ValueError:
        return None
