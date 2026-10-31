import unittest
from backend.center.source_ownership import scope_query, empty_sources


class SourceOwnershipTests(unittest.TestCase):
    def test_all_shops_queries_only_current_local_owners(self):
        self.assertEqual(
            scope_query([("page", "2"), ("shop_id", "")], {1: 2, 22: 23}), [("page", "2"), ("shop_id", "2,23")]
        )

    def test_explicit_translated_filter_is_preserved(self):
        query = [("shop_id", "23"), ("kind", "goodsdata")]
        self.assertEqual(scope_query(query, {1: 2, 22: 23}), query)

    def test_owner_change_changes_response_cache_identity(self):
        self.assertNotEqual(scope_query([], {1: 1, 2: 2}), scope_query([], {2: 2}))

    def test_node_without_owned_shops_is_empty(self):
        result = empty_sources()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["retry_count"], 0)
