"""Download PDD's observed native monthly account bill in an owned new tab."""

import logging
import asyncio, uuid
from pathlib import Path
from .report_export_adapter import note, wait, export_click
from .report_export_store import MAX_BYTES

URL = "https://yingxiao.pinduoduo.com/mains/account/report"


async def collect(page, folder):
    folder = Path(folder) / ("monthly-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    try:
        note(page, "打开多多推广财务流水页面")
        await page.call("Page.navigate", {"url": URL})
        await wait(
            page, "location.hostname==='yingxiao.pinduoduo.com'&&document.body?.innerText.includes('月账单')", 30
        )
        note(page, "切换月账单")
        await wait(
            page,
            "(()=>{const a=[...document.querySelectorAll('*')].filter(e=>e.getClientRects().length&&e.textContent.trim()==='月账单'&&!e.children.length);if(a.length!==1)return false;a[0].click();return true})()",
        )
        await wait(
            page, "document.body?.innerText.includes('月账单说明')&&document.body?.innerText.includes('本月支出')"
        )
        await page.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(folder.resolve())})
        note(page, "下载月账单明细（导入平台提供的全部月份）")
        await export_click(page, ["下载明细"])
        async with asyncio.timeout(90):
            previous = None
            while True:
                files = [p for p in folder.iterdir() if p.suffix.lower() in (".xls", ".xlsx", ".csv", ".zip")]
                partial = any(p.suffix == ".crdownload" for p in folder.iterdir())
                if len(files) > 1:
                    raise ValueError("下载生成多个文件，无法唯一核验月账单")
                if len(files) == 1 and not partial:
                    size = files[0].stat().st_size
                    if not 0 < size <= MAX_BYTES:
                        raise ValueError("月账单文件大小异常")
                    current = (files[0].name, size)
                    if current == previous:
                        target = folder.parent / "original.download"
                        files[0].replace(target)
                        return target
                    previous = current
                await asyncio.sleep(0.5)
    except TimeoutError:
        raise ValueError("下载月账单等待超时，请检查平台页面和下载权限；可安全重试") from None
    finally:
        try:
            await page.call("Browser.setDownloadBehavior", {"behavior": "default"})
        except Exception:
            logging.getLogger(__name__).debug("恢复浏览器默认下载行为失败", exc_info=True)
        # Only our own transient download folder; completed original is outside.
        import shutil

        await asyncio.to_thread(shutil.rmtree, folder, True)
