import asyncio
import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import update, delete, select
from backend.center.database import Database, snapshots, jobs, nodes, node_ids, settings, shop_ids
from backend.center.server import create_app
from backend.center.replication import encode
from backend.center.replication_worker import Delivery

ADMIN = {"Authorization": "Bearer " + "r" * 40}


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shops.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE business_cache (shop_id INTEGER, cache_key TEXT, payload TEXT, updated REAL, PRIMARY KEY(shop_id,cache_key))"
            )

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *rows):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT OR REPLACE INTO business_cache VALUES (?,?,?,?)", rows)

    def test_late_commit_behind_prior_upload_is_sent_once(self):
        self.write((1, "a", '{"v":1}', 100), (1, "b", '{"v":2}', 105))
        delivery = Delivery(self.path.parent, "business", ["test", "node"])
        records = delivery.collect()
        delivery.commit(records)
        self.assertEqual([r["scope"] for r in records], ["a", "b"])
        self.write((2, "late", '{"v":3}', 1))
        records = delivery.collect()
        delivery.commit(records)
        self.assertEqual([(r["shop"], r["scope"]) for r in records], [("2", "late")])
        self.assertEqual(delivery.collect(), [])

    def test_restart_keeps_confirmations_without_overlap_resends(self):
        self.write((1, "old", "{}", 10), (1, "recent", "{}", 990), (1, "last", "{}", 1000))
        delivery = Delivery(self.path.parent, "business", ["test", "node"])
        delivery.commit(delivery.collect())
        self.assertEqual(Delivery(self.path.parent, "business", ["test", "node"]).collect(), [])

    def test_raw_payload_is_forwarded_as_valid_json(self):
        self.write((1, "a", '{"name":"店铺","n":[1,2]}', 1))
        records = Delivery(self.path.parent, "business", ["test", "node"]).collect()
        body = json.loads(encode(records + [{"shop": "0", "scope": "s", "updated": 2.5, "payload": {"k": "v"}}]))
        self.assertEqual(body["records"][0]["payload"], {"name": "店铺", "n": [1, 2]})
        self.assertEqual(body["records"][1], {"shop": "0", "scope": "s", "updated": 2.5, "payload": {"k": "v"}})


class SuperviseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_tunnel_task_is_restarted(self):
        from backend.center.agent import supervise

        async def fail():
            raise RuntimeError("boom")

        task = asyncio.create_task(fail())
        await asyncio.gather(task, return_exceptions=True)
        started = []

        async def again():
            started.append(1)
            await asyncio.sleep(10)

        with self.assertLogs("ecmp.agent", "ERROR"):
            replacement = supervise(task, again, "业务")
        self.assertIsNot(replacement, task)
        await asyncio.sleep(0)
        self.assertEqual(started, [1])
        self.assertIs(supervise(replacement, again, "业务"), replacement)
        replacement.cancel()


class CenterReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.node = self.db.register("节点")
        self.agent = {"Authorization": "Bearer " + self.node["token"], "X-Node-ID": self.node["id"]}
        self.client = TestClient(create_app(self.db, "r" * 40))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.engine.dispose()
        self.temp.cleanup()

    def meta(self, shop="7", **payload):
        self.db.ingest(
            self.node["id"],
            [
                {
                    "shop": shop,
                    "scope": "shop_metadata",
                    "updated": 1,
                    "payload": {"name": "店铺", "platform": "pdd", **payload},
                }
            ],
        )

    def test_receipts_are_authenticated_and_node_scoped(self):
        self.meta()
        other = self.db.register("other")
        self.db.ingest(other["id"], [{"shop": "8", "scope": "private", "updated": 2, "payload": {}}])
        self.assertEqual(self.client.get("/agent/receipts").status_code, 401)
        result = self.client.get("/agent/receipts", headers=self.agent)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["records"], [{"shop": "7", "scope": "shop_metadata", "updated": 1}])

    def test_batched_ingest_keeps_newest_and_skips_retired(self):
        node = self.node["id"]
        with self.db.engine.begin() as db:
            db.execute(settings.insert().values(key=f"retired-shop:{node}:9", value={}))
        self.db.ingest(
            node,
            [
                {"shop": "1", "scope": "x", "updated": 5, "payload": {"v": 1}},
                {"shop": "1", "scope": "x", "updated": 7, "payload": {"v": 2}},
                {"shop": "1", "scope": "shop_metadata", "updated": 1, "payload": {}},
                {"shop": "9", "scope": "x", "updated": 1, "payload": {}},
            ],
        )
        self.db.ingest(
            node,
            [
                {"shop": "1", "scope": "x", "updated": 6, "payload": {"v": 3}},
                {"shop": "1", "scope": "shop_metadata", "updated": 2, "payload": {}},
            ],
        )
        self.assertEqual(self.db.list(snapshots, node=node, shop="1", scope="x")[0]["payload"], {"v": 2})
        self.assertEqual(self.db.list(snapshots, node=node, shop="9"), [])
        self.assertEqual(len(self.db.list(shop_ids)), 1)

    def test_registry_is_plain_read_and_backfills_gap(self):
        with self.db.engine.begin() as db:
            db.execute(delete(node_ids))
        self.assertEqual([n["node"] for n in self.db.registry()], [self.node["id"]])

    def test_non_ascii_credentials_are_rejected_not_crashing(self):
        bad = {"Authorization": "Bearer é".encode("latin-1")}
        self.assertEqual(self.client.get("/admin/nodes", headers=bad).status_code, 401)
        self.assertEqual(self.client.get("/api/data/products", headers=bad).status_code, 401)

    def test_malformed_shop_filter_is_bad_request(self):
        self.meta()
        response = self.client.get("/api/logs", headers=ADMIN, params={"shop_id": "abc"})
        self.assertEqual(response.status_code, 400, response.text)

    def test_revoke_closes_both_tunnels(self):
        with (
            self.client.websocket_connect("/agent/tunnel", headers=self.agent) as business,
            self.client.websocket_connect("/agent/control-tunnel", headers=self.agent) as control,
        ):
            self.assertEqual(self.client.delete("/admin/nodes/" + self.node["id"], headers=ADMIN).status_code, 200)
            self.assertEqual(business.receive()["code"], 4401)
            self.assertEqual(control.receive()["code"], 4401)

    def test_live_tunnel_is_cut_when_revoked_elsewhere(self):
        self.client.app.state.hub.recheck = 0.05
        with self.client.websocket_connect("/agent/control-tunnel", headers=self.agent) as control:
            with self.db.engine.begin() as db:
                db.execute(update(nodes).where(nodes.c.id == self.node["id"]).values(token_hash=""))
            self.assertEqual(control.receive()["code"], 4401)

    def test_binary_bodies_only_after_capability_negotiation(self):
        self.meta()
        path = "/api/invoice-imports/" + "a" * 32 + "/confirm"
        for capable in (True, False):
            headers = {**self.agent, **({"X-Tunnel-Capabilities": "binary-body"} if capable else {})}
            result = []
            with self.client.websocket_connect("/agent/tunnel", headers=headers) as node:
                if capable:
                    self.assertEqual(node.receive_json(), {"type": "hello", "capabilities": ["binary-body"]})
                thread = threading.Thread(
                    target=lambda: result.append(self.client.post(path, headers=ADMIN, json={"files": []}))
                )
                thread.start()
                message = node.receive_json()
                if capable:
                    self.assertTrue(message["binary"])
                    self.assertNotIn("body", message)
                    frame = node.receive_bytes()
                    self.assertEqual(frame[:16], uuid.UUID(hex=message["id"]).bytes)
                    self.assertEqual(json.loads(frame[16:]), {"files": []})
                    node.send_json(
                        {
                            "type": "response",
                            "id": message["id"],
                            "status": 200,
                            "binary": True,
                            "headers": {"content-type": "application/json"},
                        }
                    )
                    node.send_bytes(uuid.UUID(hex=message["id"]).bytes + b'{"ok":true}')
                else:
                    self.assertEqual(json.loads(base64.b64decode(message["body"])), {"files": []})
                    node.send_json(
                        {
                            "type": "response",
                            "id": message["id"],
                            "status": 200,
                            "body": base64.b64encode(b'{"ok":true}').decode(),
                            "headers": {"content-type": "application/json"},
                        }
                    )
                thread.join(10)
            self.assertEqual(result[0].status_code, 200, result[0].text)
            self.assertEqual(result[0].json(), {"ok": True})

    def test_move_lock_is_shared_through_database(self):
        self.meta(username="account")
        shop = self.db.shops()[0]
        with self.db.engine.begin() as db:
            db.execute(
                settings.insert().values(
                    key=f"shop-move-lock:{shop['id']}", value={"at": time.time(), "owner": "other"}
                )
            )
        target = self.db.register("目标")
        alias = next(n["id"] for n in self.db.registry() if n["node"] == target["id"])
        response = self.client.post(f"/api/shops/{shop['id']}/move-node", headers=ADMIN, json={"target_node": alias})
        self.assertEqual(response.status_code, 409)
        with self.db.engine.begin() as db:
            db.execute(
                update(settings)
                .where(settings.c.key == f"shop-move-lock:{shop['id']}")
                .values(value={"at": 0, "owner": "crashed"})
            )
        response = self.client.post(f"/api/shops/{shop['id']}/move-node", headers=ADMIN, json={"target_node": alias})
        self.assertEqual(response.status_code, 409)
        self.assertIn("均需在线", response.json()["detail"])
        with self.db.engine.connect() as db:
            self.assertIsNone(db.execute(select(settings.c.key).where(settings.c.key.like("shop-move-lock:%"))).first())

    def test_finished_jobs_are_purged_after_retention(self):
        node = self.node["id"]
        old = self.db.enqueue(node, {"kind": "overview", "shop_id": 1})
        queued = self.db.enqueue(node, {"kind": "orders", "shop_id": 1})
        with self.db.engine.begin() as db:
            db.execute(update(jobs).values(created=time.time() - 8 * 86400))
            db.execute(update(jobs).where(jobs.c.id == old).values(state="failed"))
        self.assertEqual(self.db.purge_jobs(), 1)
        self.assertEqual([j["id"] for j in self.db.list(jobs)], [queued])

    def test_product_reads_newest_product_snapshot(self):
        self.meta()
        node = self.node["id"]
        shop = self.db.shops()[0]
        self.db.ingest(
            node,
            [
                {
                    "shop": "7",
                    "scope": "products:old",
                    "updated": 10,
                    "payload": {"rows": [{"goods_id": 5, "v": "old"}]},
                },
                {
                    "shop": "7",
                    "scope": "archive:products:new",
                    "updated": 20,
                    "payload": {"rows": [{"goods_id": 5, "v": "new"}, {"goods_id": 6}]},
                },
                {
                    "shop": "7",
                    "scope": "productsX:other",
                    "updated": 30,
                    "payload": {"rows": [{"goods_id": 5, "v": "wrong"}]},
                },
            ],
        )
        response = self.client.get(f"/api/product/{shop['id']}/5", headers=ADMIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["v"] for r in response.json()["sections"]["products"]["rows"]], ["new"])

    def test_company_names_and_missing_sync_are_batched(self):
        from backend.center.business import CentralBusiness
        from backend.business import options_for

        self.meta("7")
        self.meta("8")
        self.db.ingest(
            self.node["id"],
            [
                {"shop": "7", "scope": "tax:a", "updated": 1, "payload": {"entity": {"company_name": "旧公司"}}},
                {"shop": "7", "scope": "tax:b", "updated": 2, "payload": {"entity": {"company_name": "新公司"}}},
            ],
        )
        service = CentralBusiness(self.db)
        ids = {s["local_id"]: s["id"] for s in self.db.shops()}
        self.assertEqual(service.store.company_names(), {ids[7]: "新公司"})
        result = service.data("products", options_for("products"))
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(all(s["cache_missing"] for s in result["sources"]))
        self.assertEqual(sorted(j["request"]["shop_id"] for j in self.db.list(jobs)), [7, 8])

    def test_backup_retention_keeps_newest(self):
        folder = Path(self.temp.name) / "backups"
        folder.mkdir()
        for index in range(5):
            name = f"2026101{index}-120000-abcdef.dump"
            (folder / name).write_bytes(b"x")
            (folder / name).with_suffix(".json").write_text(
                json.dumps({"name": name, "created_at": index, "size": 1, "sha256": ""}), encoding="utf-8"
            )
        (folder / "keep-me.dump").write_bytes(b"manual")

        def fake_run(arguments, **options):
            if "-f" in arguments:
                Path(arguments[arguments.index("-f") + 1]).write_bytes(b"new")

            class Result:
                returncode = 0

            return Result()

        with (
            patch.dict("os.environ", {"ECMP_CENTER_BACKUP_DIR": str(folder), "ECMP_CENTER_BACKUP_KEEP": "3"}),
            patch("backend.center.backup.subprocess.run", fake_run),
            patch("backend.center.backup.shutil.which", lambda name: name),
        ):
            with TestClient(create_app(self.db, "r" * 40)) as client:
                self.assertEqual(client.post("/api/system/backup", headers=ADMIN).status_code, 200)
                self.assertEqual(len(client.get("/api/system/maintenance", headers=ADMIN).json()["backups"]), 3)
                self.assertEqual(
                    client.put(
                        "/api/system/maintenance", headers=ADMIN, json={"enabled": False, "time": "03:30", "keep": 0}
                    ).status_code,
                    400,
                )
        self.assertEqual(len(list(folder.glob("*.dump"))), 4)
        self.assertTrue((folder / "keep-me.dump").exists())
        self.assertFalse((folder / "20261010-120000-abcdef.dump").exists())


class AgentTunnelIntegrationTests(unittest.TestCase):
    """Real websocket between the collector tunnel client and the center, in both protocol modes."""

    def test_agent_round_trip_binary_and_legacy(self):
        import httpx, uvicorn
        from backend.center import agent_tunnel

        temp = tempfile.TemporaryDirectory()
        db = Database("sqlite:///" + str(Path(temp.name) / "test.db"))
        node = db.register("节点")
        db.ingest(
            node["id"],
            [{"shop": "7", "scope": "shop_metadata", "updated": 1, "payload": {"name": "店铺", "platform": "pdd"}}],
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(db, "r" * 40), host="127.0.0.1", port=0, log_level="error", lifespan="off")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.025)
            port = server.servers[0].sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            seen = []

            def local_handler(request):
                seen.append(request.content)
                return httpx.Response(200, json={"echo": json.loads(request.content or b"null")})

            for legacy in (False, True):

                async def scenario():
                    capabilities = agent_tunnel.CAPABILITIES
                    if legacy:
                        agent_tunnel.CAPABILITIES = "none"
                    try:
                        async with httpx.AsyncClient(
                            base_url="http://127.0.0.1:1", transport=httpx.MockTransport(local_handler)
                        ) as local:
                            task = asyncio.create_task(
                                agent_tunnel.run(
                                    url,
                                    {"Authorization": "Bearer " + node["token"], "X-Node-ID": node["id"]},
                                    local,
                                    "t",
                                )
                            )
                            try:
                                async with httpx.AsyncClient(base_url=url, headers=ADMIN) as admin:
                                    for _ in range(100):
                                        if (await admin.get("/api/nodes")).json()[0]["online"]:
                                            break
                                        await asyncio.sleep(0.05)
                                    response = await admin.post(
                                        "/api/invoice-imports/" + "b" * 32 + "/confirm",
                                        json={"files": ["数据"]},
                                        timeout=10,
                                    )
                                    return response.status_code, response.json()
                            finally:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                    finally:
                        agent_tunnel.CAPABILITIES = capabilities

                self.assertEqual(asyncio.run(scenario()), (200, {"echo": {"files": ["数据"]}}))
            self.assertEqual(len(seen), 2)
        finally:
            server.should_exit = True
            thread.join(10)
            db.engine.dispose()
            temp.cleanup()
