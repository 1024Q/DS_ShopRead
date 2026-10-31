import tempfile
import unittest
import sqlite3
import json
from sqlalchemy import update
from pathlib import Path
from fastapi.testclient import TestClient
from backend.center.database import Database, snapshots, jobs
from backend.center.server import create_app


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from unittest.mock import patch

        root_patch = patch("backend.center.report_exports.ROOT", Path(self.temp.name) / "exports")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.client = TestClient(create_app(self.db, "a" * 40))
        self.node = self.db.register("Windows 采集机")
        self.headers = {"Authorization": "Bearer " + self.node["token"], "X-Node-ID": self.node["id"]}

    def tearDown(self):
        self.client.close()
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_start_releases_only_revoked_target_transfer(self):
        from backend.center.database import settings, nodes
        from backend.center.shop_transfer import release_revoked_transfer
        from sqlalchemy import select

        source = self.node["id"]
        dest = self.db.register("old target")["id"]
        self.db.ingest(
            source, [{"shop": "7", "scope": "shop_metadata", "updated": 10, "payload": {"username": "account"}}]
        )
        shop = self.db.shops()[0]
        key = f"shop-transfer:{shop['id']}"
        with self.db.engine.begin() as db:
            db.execute(settings.insert().values(key=key, value={"source": source, "target": dest}))
        self.assertFalse(release_revoked_transfer(self.db, shop))
        with self.db.engine.begin() as db:
            db.execute(update(nodes).where(nodes.c.id == dest).values(token_hash=""))
        self.assertFalse(release_revoked_transfer(self.db, {**shop, "node_id": "other"}))
        self.assertTrue(release_revoked_transfer(self.db, shop))
        with self.db.engine.connect() as db:
            self.assertIsNone(db.execute(select(settings.c.key).where(settings.c.key == key)).first())
            self.assertIsNotNone(
                db.execute(
                    select(settings.c.key).where(settings.c.key == f"shop-transfer-abandoned:{shop['id']}")
                ).first()
            )
        self.assertEqual(self.db.shops()[0]["node_id"], source)

    def test_auth_and_isolation(self):
        self.assertEqual(self.client.get("/admin/nodes").status_code, 401)
        self.assertEqual(self.client.get("/admin/nodes", headers=self.headers).status_code, 401)
        bad = {**self.headers, "X-Node-ID": "other"}
        self.assertEqual(self.client.post("/agent/claim", headers=bad).status_code, 401)

    def test_ingest_idempotent_and_no_regression(self):
        record = {"shop": "1", "scope": "overview", "updated": 20, "payload": {"sales": 100}}
        for timestamp in [20, 20, 10]:
            response = self.client.post(
                "/agent/snapshots", headers=self.headers, json={"records": [{**record, "updated": timestamp}]}
            )
            self.assertEqual(response.status_code, 200)
        rows = self.db.list(snapshots)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["updated"], 20)

    def test_jobs_deduplicate_and_lease_fencing(self):
        node = self.node["id"]
        request = {"kind": "overview", "shop_id": 1}
        job = self.db.enqueue(node, request)
        self.assertEqual(job, self.db.enqueue(node, request))
        claim = self.db.claim(node)
        self.assertIsNone(self.db.claim(node))
        self.assertFalse(self.db.finish(node, job, "wrong", {"ok": True}))
        self.assertTrue(self.db.finish(node, job, claim["lease"], {"ok": True}))
        self.assertEqual(self.db.list(jobs)[0]["state"], "dispatched")

    def test_duplicate_local_shop_ids_separate(self):
        other = self.db.register("第二台")
        record = {"shop": "1", "scope": "overview", "updated": 10, "payload": {}}
        self.db.ingest(self.node["id"], [record])
        self.db.ingest(other["id"], [record])
        self.assertEqual(len(self.db.list(snapshots)), 2)

    def test_expired_lease_cannot_complete_reclaimed_job(self):
        node = self.node["id"]
        job = self.db.enqueue(node, {"kind": "overview", "shop_id": 1})
        old = self.db.claim(node)
        with self.db.engine.begin() as db:
            db.execute(update(jobs).where(jobs.c.id == job).values(expires=0))
        new = self.db.claim(node)
        self.assertNotEqual(old["lease"], new["lease"])
        self.assertFalse(self.db.finish(node, job, old["lease"], {"ok": True}))
        self.assertTrue(self.db.finish(node, job, new["lease"], {"ok": True}))

    def test_business_page_reads_postgres_contract_without_node(self):
        from backend.center.business import CentralBusiness
        from backend.business import options_for

        meta = {
            "name": "测试店铺",
            "platform": "pdd",
            "profile_version": 0,
            "state": "logged_in",
            "operators": ["运营甲"],
        }
        self.db.ingest(self.node["id"], [{"shop": "7", "scope": "shop_metadata", "updated": 100, "payload": meta}])
        shop = self.db.shops()[0]
        service = CentralBusiness(self.db)
        key = service.key("products", options_for("products"), shop)
        self.db.ingest(
            self.node["id"],
            [
                {
                    "shop": "7",
                    "scope": key,
                    "updated": 200,
                    "payload": {
                        "complete": True,
                        "rows": [
                            {
                                "id": "123",
                                "name": "测试商品",
                                "status": "在售中",
                                "created_at": "2026-10-01",
                                "sales_30d": 5,
                            }
                        ],
                    },
                }
            ],
        )
        response = self.client.get("/api/data/products", headers={"Authorization": "Bearer " + "a" * 40})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["rows"][0]["shop_id"], shop["id"])
        self.assertNotEqual(shop["id"], 7)

    def test_machine_scope_isolation(self):
        other = self.db.register("第二台")
        for node in (self.node["id"], other["id"]):
            self.db.ingest(
                node,
                [{"shop": "1", "scope": "shop_metadata", "updated": 100, "payload": {"platform": "pdd", "name": node}}],
            )
        registry = self.db.registry()
        headers = {"Authorization": "Bearer " + "a" * 40}
        response = self.client.get(f"/api/nodes/{registry[0]['id']}/proxy/shops", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["node_id"], registry[0]["node"])

    def test_backup_manifest_with_chinese_text(self):
        from unittest.mock import patch
        import hashlib

        folder = Path(self.temp.name) / "backups"
        folder.mkdir()
        name = "20261011-120000-abcdef.dump"
        content = b"example archive"
        (folder / name).write_bytes(content)
        (folder / Path(name).with_suffix(".json")).write_text(
            json.dumps(
                {
                    "name": name,
                    "created_at": 1,
                    "size": len(content),
                    "verification": "目录校验通过",
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ECMP_CENTER_BACKUP_DIR": str(folder)}):
            client = TestClient(create_app(self.db, "a" * 40))
            headers = {"Authorization": "Bearer " + "a" * 40}
            self.assertEqual(len(client.get("/api/system/maintenance", headers=headers).json()["backups"]), 1)
            self.assertEqual(
                client.post("/api/system/backup-verify", headers=headers, params={"name": name}).status_code, 200
            )
            client.close()

    def test_revoking_node_keeps_data_and_denies_reconnect(self):
        node = self.node["id"]
        self.db.ingest(node, [{"shop": "1", "scope": "overview", "updated": 1, "payload": {}}])
        self.db.enqueue(node, {"kind": "overview", "shop_id": 1})
        response = self.client.delete("/admin/nodes/" + node, headers={"Authorization": "Bearer " + "a" * 40})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.db.authenticate(node, self.node["token"]))
        self.assertEqual(self.db.registry(), [])
        self.assertEqual(len(self.db.list(snapshots)), 1)
        self.assertEqual(self.db.list(jobs)[0]["state"], "failed")

    def test_operator_batch_routes_global_ids_to_correct_machine(self):
        from unittest.mock import AsyncMock
        import base64

        second = self.db.register("第二台")
        for node in (self.node["id"], second["id"]):
            self.db.ingest(
                node,
                [{"shop": "7", "scope": "shop_metadata", "updated": 1, "payload": {"platform": "pdd", "name": "店铺"}}],
            )
        calls = []

        async def request(node, method, path, query="", body=b""):
            if method == "POST":
                calls.append((node, json.loads(body)))
                value = {"ok": True}
            else:
                value = [{"id": 7, "platform": "pdd", "name": "店铺"}]
            return {
                "status": 200,
                "body": base64.b64encode(json.dumps(value).encode()).decode(),
                "headers": {"content-type": "application/json"},
            }

        self.client.app.state.hub.request = AsyncMock(side_effect=request)
        ids = [s["id"] for s in self.db.shops()]
        r = self.client.post(
            "/api/shops/operators",
            headers={"Authorization": "Bearer " + "a" * 40},
            json={"shop_ids": ids, "operators": ["运营甲"], "mode": "replace"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({n for n, b in calls}, {self.node["id"], second["id"]})
        self.assertTrue(all(b["shop_ids"] == [7] for n, b in calls))

    def test_move_shop_keeps_identity_history_and_fences_source(self):
        from unittest.mock import AsyncMock
        import base64

        target = self.db.register("目标机器")
        source = self.node["id"]
        dest = target["id"]
        self.db.ingest(
            source,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": 10,
                    "payload": {"username": "account", "name": "店铺", "platform": "pdd"},
                },
                {"shop": "7", "scope": "archive:test", "updated": 20, "payload": {"amount": 125}},
            ],
        )
        original = self.db.shops()[0]
        job = self.db.enqueue(source, {"kind": "overview", "shop_id": 7})
        claim = self.db.claim(source)
        calls = []

        async def remote(node, method, path, query="", body=b""):
            calls.append((node, method, path))
            value = [] if method == "GET" else {"ok": True}
            if path == "shops" and method == "POST":
                value = {"id": 12, "username": "account", "name": "店铺", "platform": "pdd", "login_method": "qr"}
            return {"status": 200, "body": base64.b64encode(json.dumps(value).encode()).decode()}

        self.client.app.state.hub.peers = {source: object(), dest: object()}
        self.client.app.state.hub.request = AsyncMock(side_effect=remote)
        alias = next(n["id"] for n in self.db.registry() if n["node"] == dest)
        headers = {"Authorization": "Bearer " + "a" * 40}
        r = self.client.post(f"/api/shops/{original['id']}/move-node", headers=headers, json={"target_node": alias})
        self.assertEqual(r.status_code, 200, r.text)
        shop = self.db.shops()[0]
        self.assertEqual(shop["id"], original["id"])
        self.assertEqual(shop["node_id"], dest)
        self.assertEqual(
            self.db.list(snapshots, node=dest, shop="12", scope="archive:test")[0]["payload"]["amount"], 125
        )
        self.db.ingest(
            source,
            [
                {"shop": "7", "scope": "shop_metadata", "updated": 99999999999, "payload": {"name": "旧节点"}},
                {"shop": "7", "scope": "archive:test", "updated": 99999999999, "payload": {}},
            ],
        )
        self.assertEqual(len(self.db.shops()), 1)
        self.assertEqual(self.db.shops()[0]["name"], "店铺")
        self.assertFalse(self.db.finish(source, job, claim["lease"], {"ok": True}))
        self.assertLess(calls.index((source, "POST", "shops/7/stop")), calls.index((dest, "POST", "shops")))
        self.assertEqual(
            self.client.post(f"/api/shops/{shop['id']}/move-node", json={"target_node": alias}).status_code, 401
        )

    def test_move_shop_rejects_offline_or_existing_target(self):
        from unittest.mock import AsyncMock
        import base64

        target = self.db.register("目标机器")
        source = self.node["id"]
        dest = target["id"]
        self.db.ingest(
            source,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": 10,
                    "payload": {"username": "account", "platform": "pdd"},
                }
            ],
        )
        shop = self.db.shops()[0]
        alias = next(n["id"] for n in self.db.registry() if n["node"] == dest)
        headers = {"Authorization": "Bearer " + "a" * 40}
        path = f"/api/shops/{shop['id']}/move-node"
        self.assertEqual(self.client.post(path, headers=headers, json={"target_node": alias}).status_code, 409)
        self.client.app.state.hub.peers = {source: object(), dest: object()}
        self.client.app.state.hub.request = AsyncMock(
            return_value={
                "status": 200,
                "body": base64.b64encode(
                    json.dumps([{"id": 12, "username": "account", "platform": "pdd"}]).encode()
                ).decode(),
            }
        )
        self.assertEqual(self.client.post(path, headers=headers, json={"target_node": alias}).status_code, 409)
        self.assertEqual(self.client.app.state.hub.request.await_count, 1)
        self.assertEqual(self.db.shops()[0]["node_id"], source)

    def test_move_disconnect_resumes_without_duplicate_creation(self):
        from unittest.mock import AsyncMock
        from fastapi import HTTPException
        import base64

        target = self.db.register("目标机器")
        source = self.node["id"]
        dest = target["id"]
        self.db.ingest(
            source,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": 10,
                    "payload": {"username": "account", "platform": "pdd"},
                }
            ],
        )
        original = self.db.shops()[0]
        alias = next(n["id"] for n in self.db.registry() if n["node"] == dest)
        headers = {"Authorization": "Bearer " + "a" * 40}
        path = f"/api/shops/{original['id']}/move-node"
        created = []
        attempts = []

        async def remote(node, method, path, query="", body=b""):
            attempts.append((method, path))
            value = created if method == "GET" else {"ok": True}
            if method == "POST" and path == "shops":
                created.append({"id": 9, "username": "account", "platform": "pdd", "name": "保留店铺"})
                raise HTTPException(504, "结果待确认")
            if method == "PUT":
                value = created[0]
            return {"status": 200, "body": base64.b64encode(json.dumps(value).encode()).decode()}

        self.client.app.state.hub.peers = {source: object(), dest: object()}
        self.client.app.state.hub.request = AsyncMock(side_effect=remote)
        self.assertEqual(self.client.post(path, headers=headers, json={"target_node": alias}).status_code, 504)
        self.assertEqual(self.db.shops()[0]["node_id"], source)
        self.db.enqueue(source, {"kind": "overview", "shop_id": 7})
        self.assertIsNone(self.db.claim(source))
        blocked = self.client.post(f"/api/shops/{original['id']}/start", headers=headers, json={})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.client.post(path, headers=headers, json={"target_node": alias}).status_code, 200)
        self.assertEqual(attempts.count(("POST", "shops")), 1)
        self.assertEqual(self.db.shops()[0]["id"], original["id"])

    def test_identify_node_by_token_without_node_id(self):
        headers = {"Authorization": "Bearer " + self.node["token"]}
        response = self.client.get("/agent/identify", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.node["id"])
        self.assertNotIn("token", response.json())
        self.assertFalse(response.json()["connected"])
        self.assertEqual(self.client.get("/agent/identify").status_code, 401)
        self.assertEqual(
            self.client.get("/agent/identify", headers={"Authorization": "Bearer " + "a" * 40}).status_code, 401
        )
        self.client.delete("/admin/nodes/" + self.node["id"], headers={"Authorization": "Bearer " + "a" * 40})
        self.assertEqual(self.client.get("/agent/identify", headers=headers).status_code, 401)

    def test_transfer_saved_password_is_private_and_not_persisted(self):
        from unittest.mock import AsyncMock
        import base64

        target = self.db.register("destination")
        source = self.node["id"]
        dest = target["id"]
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
        original = self.db.shops()[0]
        sent = []
        started = []

        async def remote(node, method, path, query="", body=b""):
            value = [] if method == "GET" else {"ok": True}
            if path.endswith("/transfer-credentials"):
                value = {"password": "fixture-password"}
            if path == "shops" and method == "POST":
                sent.append(json.loads(body))
                value = {"id": 12, "username": "account", "platform": "pdd"}
            if path == "shops/12/start":
                started.append(self.db.shops()[0]["node_id"])
                value = {"state": "needs_verification"}
            return {"status": 200, "body": base64.b64encode(json.dumps(value).encode()).decode()}

        hub = self.client.app.state.hub
        hub.peers = {source: object(), dest: object()}
        hub.request = AsyncMock(side_effect=remote)
        alias = next(n["id"] for n in self.db.registry() if n["node"] == dest)
        headers = {"Authorization": "Bearer " + "a" * 40}
        blocked = self.client.post(f"/api/shops/{original['id']}/transfer-credentials", headers=headers)
        self.assertEqual(blocked.status_code, 404)
        hub.request.assert_not_called()
        result = self.client.post(
            f"/api/shops/{original['id']}/move-node",
            headers=headers,
            json={"target_node": alias, "login_method": "password"},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(sent[0]["password"], "fixture-password")
        self.assertNotIn("fixture-password", result.text)
        self.assertNotIn("fixture-password", str(self.db.list(snapshots)))
        self.assertEqual(started, [dest])
        self.assertEqual(result.json()["login_state"], "needs_verification")
        self.assertTrue(result.json()["ok"])

    def test_missing_saved_password_does_not_stop_or_move_shop(self):
        from unittest.mock import AsyncMock
        import base64

        target = self.db.register("destination")
        source = self.node["id"]
        dest = target["id"]
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
        calls = []

        async def remote(node, method, path, query="", body=b""):
            calls.append(path)
            status = 409 if path.endswith("/transfer-credentials") else 200
            value = {"detail": "未保存密码"} if status == 409 else []
            return {"status": status, "body": base64.b64encode(json.dumps(value).encode()).decode()}

        hub = self.client.app.state.hub
        hub.peers = {source: object(), dest: object()}
        hub.request = AsyncMock(side_effect=remote)
        alias = next(n["id"] for n in self.db.registry() if n["node"] == dest)
        result = self.client.post(
            f"/api/shops/{shop['id']}/move-node",
            headers={"Authorization": "Bearer " + "a" * 40},
            json={"target_node": alias, "login_method": "password"},
        )
        self.assertEqual(result.status_code, 409)
        self.assertNotIn("shops/7/stop", calls)
        self.assertEqual(self.db.shops()[0]["node_id"], source)
