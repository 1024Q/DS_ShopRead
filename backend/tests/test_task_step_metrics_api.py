import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.center.database import Database
from backend.center.server import create_app


class StepMetricsApiTests(unittest.TestCase):
    def test_metrics_are_requested_explicitly_and_merged_across_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database("sqlite:///" + str(Path(folder) / "center.db"))
            self.addCleanup(database.engine.dispose)
            nodes = [database.register("甲"), database.register("乙")]
            app = create_app(database, "x" * 40)
            queries = []

            async def request(node, method, path, query="", body=b""):
                queries.append(query)
                result = {"collection": {"max_concurrent": 20}}
                if "include_step_metrics=true" in query:
                    first = node == nodes[0]["id"]
                    result.update(
                        step_metrics={
                            "pdd": {
                                "overview@base": {
                                    "sample_count": 2 if first else 1,
                                    "estimated_read_seconds": 10 if first else 40,
                                    "last_read_seconds": 8 if first else 40,
                                    "sampled_at": 100 if first else 110,
                                }
                            }
                        },
                        step_metrics_status={"complete": True, "unavailable_shops": 0, "updated_at": 200},
                    )
                return {
                    "status": 200,
                    "body": base64.b64encode(json.dumps(result).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                headers = {"Authorization": "Bearer " + "x" * 40}
                ordinary = client.get("/api/system/settings", headers=headers)
                self.assertEqual(ordinary.status_code, 200, ordinary.text)
                self.assertNotIn("step_metrics", ordinary.json())
                self.assertTrue(all(query == "" for query in queries))
                queries.clear()
                measured = client.get(
                    "/api/system/settings?include_step_metrics=true&metrics_platform=pdd", headers=headers
                )
                self.assertEqual(measured.status_code, 200, measured.text)
                result = measured.json()
                value = result["step_metrics"]["pdd"]["overview@base"]
                self.assertEqual(value["sample_count"], 3)
                self.assertEqual(value["estimated_read_seconds"], 20)
                self.assertTrue(result["step_metrics_status"]["complete"])
                self.assertEqual(len(queries), 2)
                self.assertTrue(all("metrics_platform=pdd" in query for query in queries))
                single = client.get(
                    "/api/nodes/1/proxy/system/settings?include_step_metrics=true&metrics_platform=pdd",
                    headers=headers,
                ).json()
                self.assertEqual(single["step_metrics"]["pdd"]["overview@base"]["sample_count"], 2)
            database.engine.dispose()


if __name__ == "__main__":
    unittest.main()
