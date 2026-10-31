import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from backend.collector_lease import ownership, owns


class LeaseIdentityTests(unittest.TestCase):
    def test_cleanup_authority_tracks_actual_exclusive_lease(self):
        @contextmanager
        def shop_db(sid, *, write=False):
            self.assertEqual(sid, 1)
            self.assertTrue(write)
            yield SimpleNamespace(execute=lambda *_: None)

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store = SimpleNamespace(directory=directory, list=lambda: [{"id": 1}], shop_db=shop_db)
            self.assertFalse(owns(directory))
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with ownership(store):
                    self.assertTrue(owns(directory))
                    self.assertTrue(owns(directory.resolve()))
                    from backend.reader_targets import register, forget

                    session = SimpleNamespace(store=store, shop={"id": 1}, profile=directory / "profile")
                    record = register(
                        session, "ws://127.0.0.1/devtools/browser/11111111-1111-4111-8111-111111111111", "reader"
                    )
                    self.assertIsNotNone(record)
                    forget(session, record)
                    raise RuntimeError("stop")
            self.assertFalse(owns(directory))


if __name__ == "__main__":
    unittest.main()
