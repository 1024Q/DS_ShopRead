import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import promotion_logs, promotions, request_reuse
from backend.collector_logs import BusinessTail, shop_event
from backend.store import Store


class NativeCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_rate_limit_response_stops_next_navigation_before_browser_creation(self):
        session = SimpleNamespace()
        page = promotions.PromotionPage(session)
        page.response((promotions.LOGS, {}, page.response_generation), {'success': False, 'errorMsg': '请求频繁，请稍后重试'})
        self.assertGreater(request_reuse.cooling(session, promotions.LOGS), 290)
        with patch('backend.promotions.PromotionPage') as constructor:
            with self.assertRaisesRegex(ValueError, '冷却'):
                await promotion_logs.collect(session, {'date_from': '2026-10-24', 'date_to': '2026-10-30'})
            constructor.assert_not_called()

    async def test_stale_browser_response_does_not_cool_current_generation(self):
        session = SimpleNamespace()
        page = promotions.PromotionPage(session)
        page.response((promotions.LOGS, {}, page.response_generation - 1), {'errorMsg': '请求频繁'})
        self.assertEqual(request_reuse.cooling(session, promotions.LOGS), 0)


class LogNamesTests(unittest.TestCase):
    def test_maintenance_does_not_write_or_prune_legacy_database_logs(self):
        from backend.database_maintenance import Maintenance
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect() as db:
                db.execute("INSERT INTO event_logs(created,last_seen,level,module,event,shop_name,message) VALUES(1,1,'info','system','old','old','keep')")
            result = Maintenance(store).prune()
            self.assertEqual(result['logs']['storage'], 'files')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM event_logs').fetchone()[0], 1)

    def test_identity_cache_survives_temporary_name_read_failure(self):
        from backend.log_identity import identity
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.add('test-account', 'pw', name='测试旗舰店')
            self.assertEqual(identity(store.path, sid), ('测试旗舰店', 'pdd'))
            import sqlite3
            with patch('backend.log_identity.sqlite3.connect', side_effect=sqlite3.OperationalError('busy')):
                store.log_event('info', 'orders', 'sync_success', '读取完成', sid)
            logs = list((Path(directory) / 'launcher-logs').glob('测试旗舰店-shop-*.log'))
            self.assertTrue(logs)
            self.assertFalse(list((Path(directory) / 'launcher-logs').glob('未命名*')))

    def test_panel_resolves_old_nameless_records_without_rewriting_history(self):
        from backend.collector_logs import BEIJING
        from datetime import datetime
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.add('test-account', 'pw', name='测试店')
            folder = Path(directory) / 'log-spool'; folder.mkdir(exist_ok=True)
            file = folder / (datetime.now(BEIJING).strftime('%Y-%m-%d') + '-fixture.jsonl')
            raw = json.dumps({'created': 1, 'module': 'orders', 'level': 'info', 'event': 'sync_success', 'shop_id': sid, 'message': 'done'}) + '\n'
            file.write_text(raw, encoding='utf-8')
            self.assertIn('拼多多 · 测试店', BusinessTail(store.path).read()[0][1])
            self.assertEqual(file.read_text(encoding='utf-8'), raw)
            shop_event(Path(directory) / 'launcher-logs', sid, 'info', 'orders', 'event', 'done', '')
            self.assertFalse(list((Path(directory) / 'launcher-logs').glob('未命名*')))
