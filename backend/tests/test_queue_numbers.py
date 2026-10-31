import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.master_tasks import project
from backend.queue_eta import estimates
from backend.tests.collection_fixture import policy


class QueueNumberTests(unittest.TestCase):
    def service(self, identity):
        return SimpleNamespace(
            queue_namespace=identity,
            queue_config=policy(max_concurrent=2, max_active_shops=2, shop_concurrent=1),
            progress={
                (1, "reviews:a"): {"kind": "reviews", "scope": {}, "phase": "queued"},
                (2, "tickets:b"): {"kind": "tickets", "scope": {}, "phase": "queued"},
            },
        )

    def test_node_and_parent_child_namespaces_cannot_collide(self):
        first, second = self.service("node-a"), self.service("node-b")
        a, b = estimates(first, 100), estimates(second, 100)
        self.assertFalse({v["queue_number"] for v in a.values()} & {v["queue_number"] for v in b.values()})
        with patch("backend.queue_eta.estimates", return_value=a):
            parents = project(first)
        self.assertEqual(sorted(r["queue_position"] for r in parents), [1, 2])
        self.assertEqual(len({r["queue_number"] for r in parents}), 2)
        self.assertFalse({r["queue_number"] for r in parents} & {v["queue_number"] for v in a.values()})


if __name__ == "__main__":
    unittest.main()
