import asyncio, json, sys
from pathlib import Path
from backend.store import Store
from backend.browser import Session
from backend.business_reader import ReadPage


async def main():
    store = Store()
    session = Session(store.list()[0], store)
    kind = sys.argv[1]
    async with ReadPage(session) as page:
        await page.open(kind)
        await asyncio.sleep(3)
        if kind == "intercepts":
            cached = json.loads(Path(".data/live-1-intercepts.json").read_text(encoding="utf8"))
            for status in ("状态 0", "状态 4", "状态 5"):
                row = next(r for r in cached["rows"] if r["status"] == status)
                await page.evaluate(
                    "(()=>{const e=document.querySelector('input[placeholder=\"请输入完整订单编号\"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,"
                    + json.dumps(row["order_number"])
                    + ");e.dispatchEvent(new Event('input',{bubbles:true}));})()"
                )
                await asyncio.sleep(0.2)
                await page.evaluate(
                    "[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='查询').click()"
                )
                await asyncio.sleep(1)
                print(
                    status,
                    await page.evaluate("[...document.querySelectorAll('table')].map(e=>e.innerText).join('\\n')"),
                    flush=True,
                )
            return
        if kind in ("packages", "aftersales", "invoices", "navigator"):
            page.reader.cancel()
            await asyncio.gather(page.reader, return_exceptions=True)

            async def receiver():
                async for raw in page.ws:
                    message = json.loads(raw)
                    if message.get("id") in page.pending:
                        future = page.pending.pop(message["id"])
                        if not future.done():
                            future.set_result(message)
                    if message.get("method") == "Network.requestWillBeSent":
                        request = message["params"]["request"]
                        if any(
                            s in request["url"]
                            for s in (
                                "/new/pack/center/list/get",
                                "/afterSales/queryList",
                                "/invoice_list",
                                "/queryGoodsNavigatorList",
                            )
                        ):
                            print("QUERY", request.get("postData"), flush=True)

            page.reader = asyncio.create_task(receiver())
            if kind == "navigator":
                await page.evaluate("document.querySelector('[data-testid=beast-core-pagination-next]').click()")
                await asyncio.sleep(1)
                return
            if kind != "packages":
                await page.evaluate(
                    "[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='查询').click()"
                )
                await asyncio.sleep(1)
                return
            for label in ["揽件未中转", "中转未派件", "派件未签收", "已签收"]:
                await page.evaluate(
                    "[...document.querySelectorAll('[data-testid=beast-core-select]')].find(e=>e.offsetWidth>0).click()"
                )
                await asyncio.sleep(0.2)
                await page.evaluate(
                    "[...document.querySelectorAll('[role=option]')].find(e=>e.textContent.trim()==="
                    + json.dumps(label)
                    + ").click()"
                )
                await page.evaluate(
                    "[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='查询').click()"
                )
                await asyncio.sleep(0.8)
                print(label, flush=True)
        else:
            print(
                await page.evaluate(
                    "({buttons:[...document.querySelectorAll('input')].filter(e=>e.offsetWidth>0).map(e=>({placeholder:e.placeholder,value:e.value,html:e.outerHTML.slice(0,400)})),links:[...document.querySelectorAll('a[href]')].filter(e=>e.innerText==='下一页').map(e=>e.outerHTML)})"
                )
            )


asyncio.run(main())
