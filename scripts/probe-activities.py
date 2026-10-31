"""Inspect read-only marketing pages; never click enrollment or withdrawal actions."""

import asyncio, json, sys
from pathlib import Path
from urllib.parse import urlsplit
from backend.store import Store
from backend.browser import Session
from backend.business_reader import ReadPage, PAGES


async def main():
    store = Store()
    session = Session(store.list()[0], store)
    PAGES["activities"] = "https://mms.pinduoduo.com/act/index?SourceType=MMSActicon"
    if len(sys.argv) > 1:
        PAGES["activities"] = sys.argv[1]
    requests = {}
    responses = {}
    tasks = []
    async with ReadPage(session) as page:
        page.reader.cancel()
        await asyncio.gather(page.reader, return_exceptions=True)

        async def body(rid):
            try:
                result = await page.call("Network.getResponseBody", {"requestId": rid})
                responses[rid] = json.loads(result["body"])
            except Exception:
                pass

        async def receive():
            async for raw in page.ws:
                m = json.loads(raw)
                p = m.get("params", {})
                if m.get("id") in page.pending:
                    future = page.pending.pop(m["id"])
                    if not future.done():
                        future.set_result(m)
                if m.get("method") == "Network.requestWillBeSent":
                    r = p["request"]
                    url = urlsplit(r["url"])
                    if p.get("type") in ("XHR", "Fetch") and url.hostname == "mms.pinduoduo.com":
                        requests[p["requestId"]] = {
                            "path": url.path,
                            "query": url.query,
                            "method": r["method"],
                            "body": r.get("postData"),
                        }
                if m.get("method") == "Network.loadingFinished" and p["requestId"] in requests:
                    task = asyncio.create_task(body(p["requestId"]))
                    tasks.append(task)

        page.reader = asyncio.create_task(receive())
        await page.open("activities")
        await asyncio.sleep(5)
        if len(sys.argv) > 2:
            label = sys.argv[2]
            if label not in (
                "活动报名记录",
                "报名记录",
                "审核中",
                "已报名",
                "已驳回",
                "已结束",
                "下一页",
                "查询",
                "报名状态",
                "活动类型",
                "全部",
            ):
                raise ValueError("非只读菜单")
            if label == "下一页":
                print(
                    "click",
                    await page.evaluate(
                        "(()=>{const e=document.querySelector('[data-testid=beast-core-pagination-next]');if(e)e.click();return !!e;})()"
                    ),
                )
            else:
                print(
                    "click",
                    await page.evaluate(
                        "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.offsetWidth>0&&e.textContent.trim()==="
                        + json.dumps(label)
                        + ");if(e)e.click();return !!e;})()"
                    ),
                )
            await asyncio.sleep(3)
        dom = await page.evaluate(
            "({url:location.href,text:document.body.innerText,links:[...document.querySelectorAll('a')].filter(e=>/活动|报名|下一页/.test(e.innerText)).map(e=>({text:e.innerText,href:e.getAttribute('href')})),inputs:[...document.querySelectorAll('input')].filter(e=>e.offsetWidth>0).map(e=>({placeholder:e.placeholder,value:e.value})),selects:[...document.querySelectorAll('select')].map(e=>e.innerText)})"
        )
        await asyncio.gather(*tasks, return_exceptions=True)
        # Store only activity read responses, without headers/cookies/auth payloads.
        network = [
            {**r, "response": responses.get(rid)}
            for rid, r in requests.items()
            if any(s in r["path"].lower() for s in ("act/", "activity", "enroll", "signup", "marketing"))
            and not any(s in r["path"].lower() for s in ("auth", "token", "login"))
        ]
        Path(".data/activity-probe.json").write_text(
            json.dumps({"dom": dom, "network": network}, ensure_ascii=False), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "url": dom["url"],
                    "content": dom["text"].split("活动报名记录")[-1][:2200],
                    "requests": [
                        {k: v for k, v in r.items() if k != "response"}
                        for r in network
                        if r["path"] == "/lakemms/activityGoods/list"
                    ],
                },
                ensure_ascii=False,
            )
        )


asyncio.run(main())
