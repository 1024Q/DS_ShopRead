import base64, json
from unittest.mock import AsyncMock
from backend.tests.test_center_architecture import CenterTests
from backend.center.database import settings


class RetargetTests(CenterTests):
    def test_retarget_checks_old_destination_and_write_phase(self):
        source = self.node["id"]
        old = self.db.register("d02")["id"]
        new = self.db.register("d03")["id"]
        self.db.ingest(
            source,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": 1,
                    "payload": {"username": "account", "platform": "pdd"},
                }
            ],
        )
        shop = self.db.shops()[0]
        key = f"shop-transfer:{shop['id']}"
        alias = next(n["id"] for n in self.db.registry() if n["node"] == new)
        hub = self.client.app.state.hub
        hub.peers = {source: object(), old: object(), new: object()}
        old_rows = []
        new_rows = []
        calls = []

        async def remote(node, method, path, query="", body=b""):
            calls.append((node, method, path))
            value = (
                (old_rows if node == old else new_rows)
                if method == "GET"
                else {"id": 10, "state": "logged_in", "platform": "pdd", "username": "account"}
            )
            return {"status": 200, "body": base64.b64encode(json.dumps(value).encode()).decode()}

        hub.request = AsyncMock(side_effect=remote)

        def pending(phase):
            with self.db.engine.begin() as db:
                db.execute(settings.delete().where(settings.c.key == key))
                db.execute(settings.insert().values(key=key, value={"source": source, "target": old, "phase": phase}))

        def move():
            return self.client.post(
                f"/api/shops/{shop['id']}/move-node",
                headers={"Authorization": "Bearer " + "a" * 40},
                json={"target_node": alias},
            )

        pending("target_write_started")
        r = move()
        self.assertEqual(r.status_code, 409)
        self.assertIn("d02", r.json()["detail"])
        self.assertEqual(calls, [])
        pending("source_stopping")
        old_rows.append({"id": 2, "username": "account", "platform": "pdd"})
        self.assertEqual(move().status_code, 409)
        old_rows.clear()
        new_rows.append({"id": 3, "username": "account", "platform": "pdd"})
        self.assertEqual(move().status_code, 409)
        with self.db.engine.connect() as db:
            self.assertEqual(db.execute(settings.select().where(settings.c.key == key)).first().value["target"], old)
        new_rows.clear()
        r = move()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.db.shops()[0]["node_id"], new)
        self.assertNotIn((old, "POST", "shops"), calls)
