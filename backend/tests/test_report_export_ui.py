import asyncio, json, subprocess, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import report_export_ui as ui, report_export_adapter as adapter
from backend.read_errors import PageReadError
from backend.report_export_store import FIELDS
from backend.tests.test_report_exports import scope


class WaitingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_recovery_is_once_only_and_never_resubmits_platform_intent(self):
        import tempfile
        from backend.report_exports import Worker

        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(SimpleNamespace(directory=Path(folder)), None)
            jobs = []
            for sid, submitted in ((1, None), (2, time.time())):
                job = worker.ledger.create({"id": sid}, *scope())
                worker.ledger.update(
                    job["id"],
                    state="exception",
                    stage="打开配置字段",
                    message="导出字段勾选核验失败",
                    submitted_at=submitted,
                )
                jobs.append(job)
            self.assertEqual(worker.recover_ui_jobs(), 1)
            self.assertEqual(worker.ledger.get(jobs[0]["id"])["state"], "queued")
            self.assertEqual(worker.ledger.get(jobs[1]["id"])["state"], "exception")
            worker.ledger.update(jobs[0]["id"], state="exception")
            self.assertEqual(worker.recover_ui_jobs(), 0)

    async def test_retry_with_empty_submission_timestamp_does_not_stick_in_queue(self):
        import tempfile
        from backend.report_exports import Worker
        from backend.shop_lock import ShopLock

        with tempfile.TemporaryDirectory() as folder:
            item = SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", lock=ShopLock())
            worker = Worker(SimpleNamespace(directory=Path(folder)), lambda _: item)
            job = worker.ledger.create(item.shop, *scope())
            job = worker.ledger.update(job["id"], submitted_at=None)
            with patch.object(adapter, "OrdersPage", side_effect=ValueError("已进入页面步骤")):
                await worker.process(job)
            self.assertEqual(worker.ledger.get(job["id"])["message"], "已进入页面步骤")

    async def test_transient_ready_state_is_not_enough_and_guide_is_checked_each_time(self):
        reads = iter([False, True, False, True, True])
        calls = []

        async def evaluate(expression):
            calls.append(expression)
            return False if expression in (ui.GUIDE, ui.NOTICE, ui.SHIPPING_NOTICE) else next(reads)

        page = SimpleNamespace(evaluate=evaluate)
        with patch.object(ui.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await ui.state(page, "ready", "等待弹窗"))
        self.assertEqual(calls.count("ready"), 5)
        self.assertEqual(calls.count(ui.GUIDE), 5)

    async def test_timeout_reports_exact_wait_stage(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=False))
        with self.assertRaisesRegex(PageReadError, "等待字段弹窗关闭"):
            await ui.state(page, "ready", "等待字段弹窗关闭", seconds=0)

    async def test_export_timezone_set_before_opening_merchant_page(self):
        page = adapter.OrdersPage(SimpleNamespace())
        page.call = AsyncMock()
        page.__aexit__ = AsyncMock()
        with patch.object(adapter.BaseOrdersPage, "__aenter__", AsyncMock(return_value=page)):
            self.assertIs(await page.__aenter__(), page)
        page.call.assert_awaited_once_with("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})


class NativeControlTests(unittest.TestCase):
    def test_duplicate_background_control_and_div_guide_do_not_capture_the_click(self):
        node = Path(__file__).resolve().parents[2] / ".runtime/node.exe"
        fixture = """let clicked=[];
   function element(name,id,visible=true){return {innerText:name,children:[],disabled:false,
    getClientRects:()=>[{}],closest:()=>null,getAttribute:()=>null,scrollIntoView:()=>{},
    getBoundingClientRect:()=>({x:id,y:0,width:1,height:1}),contains:()=>false,click:()=>clicked.push(id)}}
   const behind=element('修改报表字段',1),actual=element('修改报表字段',2),guide=element('我知道了',3);
   const document={body:{innerText:'导出订单可以选择报表类型啦'},querySelectorAll:()=>[actual,behind,guide],elementFromPoint:x=>x<2?{}:x<3?actual:guide};
  """
        code = (
            fixture
            + "console.log(JSON.stringify(["
            + ui.click_script(["配置字段", "添加报表字段", "修改报表字段"])
            + ","
            + ui.GUIDE
            + ",clicked]));"
        )
        result = subprocess.run([str(node), "-e", code], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, True, [2, 3]])

    def test_shipping_notice_only_closes_known_reminder(self):
        node = Path(__file__).resolve().parents[2] / ".runtime/node.exe"
        fixture = """let closed=0,processed=0;
   const close={innerText:'',getClientRects:()=>[{}],getBoundingClientRect:()=>({x:0,y:0,width:2,height:2}),contains:()=>false,click:()=>closed++};
   const process={...close,innerText:'去处理',click:()=>processed++};
   const box={innerText:'您有1笔订单未在承诺发货时间内发货或揽收，请及时处理\\n去处理',getClientRects:()=>[{}],querySelectorAll:()=>[process,close]};
   const document={querySelectorAll:()=>[box],elementFromPoint:()=>close};
  """
        code = (
            fixture
            + "const a="
            + ui.SHIPPING_NOTICE
            + ";box.innerText='账号安全验证 去处理';const b="
            + ui.SHIPPING_NOTICE
            + ";console.log(JSON.stringify([a,b,closed,processed]));"
        )
        result = subprocess.run([str(node), "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout), [True, False, 1, 0])

    def test_match_accepts_numeric_strings_and_milliseconds_but_rejects_shifted_ranges(self):
        from datetime import datetime
        from backend.business_reader import BEIJING

        start, end = scope()
        now = time.time()
        job = {"start": start, "end": end, "fields": FIELDS, "submitted_at": now}
        row = {
            "id": "n",
            "groupStartTime": str(int(datetime.fromisoformat(start).replace(tzinfo=BEIJING).timestamp() * 1000)),
            "groupEndTime": str(int(datetime.fromisoformat(end).replace(tzinfo=BEIJING).timestamp() * 1000)),
            "orderType": "0",
            "afterSaleType": "1",
            "createdTime": str(int(now * 1000)),
            "titles": list(adapter.NEW_FIELD_IDS),
        }
        self.assertTrue(adapter.match(row, job))
        shifted = {**row, "groupStartTime": int(row["groupStartTime"]) - 43200000}
        self.assertFalse(adapter.match(shifted, job))
        self.assertTrue(any("时区" in e for e in adapter.match_errors(shifted, job)))


class ClockTests(unittest.TestCase):
    def test_confirmed_submission_uses_new_ids_not_node_clock(self):
        from datetime import datetime
        from backend.business_reader import BEIJING

        start, end = scope()
        now = time.time()
        job = {
            "start": start,
            "end": end,
            "fields": FIELDS,
            "submitted_at": now,
            "submission_confirmed": True,
            "before_ids": ["old"],
        }
        row = {
            "id": "new",
            "groupStartTime": int(datetime.fromisoformat(start).replace(tzinfo=BEIJING).timestamp()),
            "groupEndTime": int(datetime.fromisoformat(end).replace(tzinfo=BEIJING).timestamp()),
            "orderType": 0,
            "afterSaleType": 1,
            "createdTime": now - 63,
            "titles": list(adapter.NEW_FIELD_IDS),
        }
        self.assertTrue(adapter.match(row, job))
        self.assertFalse(adapter.match({**row, "id": "old"}, job))
        self.assertFalse(adapter.match({**row, "afterSaleType": 0}, job))
        self.assertFalse(adapter.match({**row, "titles": []}, job))
        self.assertFalse(adapter.match(row, {**job, "submission_confirmed": False}))
        self.assertFalse(adapter.match(row, {k: v for k, v in job.items() if k != "before_ids"}))
        self.assertEqual(adapter.match_diagnostics([{**row, "id": "old"}, row], job)[0]["reasons"], [])
