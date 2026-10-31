import asyncio
import json
import sys
from pathlib import Path
import httpx
import websockets

URLS = {
    "products": "https://mms.pinduoduo.com/goods/goods_list",
    "tickets": "https://mms.pinduoduo.com/aftersales/work_order/list",
    "reviews": "https://mms.pinduoduo.com/goods/evaluation/index",
}
URLS["overview"] = "https://mms.pinduoduo.com/home/"
URLS["transactions"] = "https://mms.pinduoduo.com/sycm/stores_data"
URLS["goodsdata"] = "https://mms.pinduoduo.com/sycm/goods_effect"
URLS["entity"] = "https://mms.pinduoduo.com/mallcenter/mallinfo"
URLS["reconciliation"] = "https://mms.pinduoduo.com/cashier/finance/payment-bills"
URLS.update(
    {
        key: "https://mms.pinduoduo.com" + path
        for key, path in {
            "packages": "/express/package",
            "invoices": "/invoice/center",
            "aftersales": "/aftersales/aftersale_list",
            "evidence": "/aftersales/merchant_appeal",
            "intercepts": "/orders/interceptExpress",
            "navigator": "/sycm/goods_quality/pilot_goods",
            "quality": "/pg/mall_quality",
            "brands": "/kit/brand-price-change?tool_full_channel=10323_97807",
        }.items()
    }
)
CLICKS = {
    "brands-none": ["无黑标商品"],
    "aftersales-merchant": ["平台介入待商家处理"],
    "aftersales-platform": ["平台处理中"],
    "aftersales-pending": ["待商家处理"],
    "aftersales-all": ["全部"],
    "evidence-pending": ["待举证"],
    "evidence-overdue": ["超时未举证"],
    "evidence-submitted": ["已举证"],
    "intercepts-failed": ["拦截失败"],
    "quality-violations": ["品质违规商品"],
    "quality-others": ["其他商品"],
    "invoices-success": ["开票成功"],
    "invoices-failed": ["开票失败"],
    "invoices-skipped": ["暂不开票"],
}
CLICKS.update(
    {
        "reconciliation-day": ["日汇总"],
        "reconciliation-month": ["月汇总"],
        "reconciliation-expanded": ["月汇总", "展开"],
        "reconciliation-day-expanded": ["日汇总", "展开"],
        "entity-subject": ["主体信息"],
    }
)
CLICKS.update({"transactions-yesterday": ["昨日"], "transactions-week": ["7日"], "transactions-month": ["30日"]})
CLICKS.update({"goodsdata-week": ["7日"], "goodsdata-month": ["30日"], "goodsdata-yesterday": ["昨日"]})
for key in CLICKS:
    URLS[key] = URLS[key.split("-")[0]]


async def inspect(kind, url):
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        result = (await client.put("http://127.0.0.1:9222/json/new?about:blank")).json()
        target_id = result["id"]
        async with websockets.connect(result["webSocketDebuggerUrl"], max_size=30_000_000) as ws:
            pending = {}
            events = []
            seq = 0

            async def receive():
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("id") in pending:
                        pending.pop(message["id"]).set_result(message)
                    elif message.get("method", "").startswith("Network."):
                        events.append(message)

            reader = asyncio.create_task(receive())

            async def call(method, params={}):
                nonlocal seq
                seq += 1
                future = asyncio.get_running_loop().create_future()
                pending[seq] = future
                await ws.send(json.dumps({"id": seq, "method": method, "params": params}))
                answer = await asyncio.wait_for(future, 20)
                if "error" in answer:
                    raise RuntimeError(answer["error"])
                return answer.get("result", {})

            try:
                await call("Network.enable", {"maxTotalBufferSize": 30_000_000, "maxResourceBufferSize": 5_000_000})
                await call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
                await call("Page.navigate", {"url": url})
                await asyncio.sleep(6)
                for label in CLICKS.get(kind, []):
                    clicked = await call(
                        "Runtime.evaluate",
                        {
                            "expression": "(()=>{const es=[...document.querySelectorAll('*')].filter(e=>e.offsetWidth>0&&e.children.length===0);const e=es.find(e=>e.textContent.trim()==="
                            + json.dumps(label)
                            + ")||es.find(e=>e.textContent.trim().startsWith("
                            + json.dumps(label)
                            + ")&&e.textContent.trim().length<30);if(e)e.click();return !!e;})()",
                            "returnByValue": True,
                        },
                    )
                    print(kind, "click", label, clicked.get("result", {}).get("value"), flush=True)
                    await asyncio.sleep(2)
                if kind in ("products", "reviews"):
                    texts = ["全部"] if kind == "products" else ["1星", "2星", "3星", "未回复", "查询"]
                    for text in texts:
                        await call(
                            "Runtime.evaluate",
                            {
                                "expression": "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.trim()==="
                                + json.dumps(text)
                                + "&&e.offsetWidth>0);if(e)e.click();return !!e;})()",
                                "returnByValue": True,
                            },
                        )
                        await asyncio.sleep(0.3)
                    await asyncio.sleep(2)
                dom = await call(
                    "Runtime.evaluate",
                    {
                        "expression": "({font:[...document.querySelectorAll('style')].map(e=>e.textContent).find(t=>t.includes('spider-font')),url:location.href,text:document.body.innerText.slice(-14000),tables:[...document.querySelectorAll('table')].slice(0,4).map(t=>({html:t.outerHTML.slice(0,12000),text:t.innerText})),buttons:[...document.querySelectorAll('button,[role=tab],input')].map(e=>({tag:e.tagName,text:e.innerText,placeholder:e.placeholder,class:e.className})).slice(-80)})",
                        "returnByValue": True,
                    },
                )
                requests = {
                    event["params"]["requestId"]: event["params"]["request"]
                    for event in events
                    if event["method"] == "Network.requestWillBeSent"
                }
                bodies = []
                for event in list(events):
                    if event["method"] != "Network.responseReceived":
                        continue
                    params = event["params"]
                    response = params["response"]
                    if params.get("type") not in ("XHR", "Fetch") or "json" not in response.get("mimeType", ""):
                        continue
                    try:
                        body = await call("Network.getResponseBody", {"requestId": params["requestId"]})
                        data = json.loads(body["body"])
                        bodies.append(
                            {
                                "url": response["url"],
                                "request": {
                                    k: v
                                    for k, v in requests.get(params["requestId"], {}).items()
                                    if k in ("url", "method", "postData")
                                },
                                "data": data,
                            }
                        )
                    except Exception:
                        pass
                Path(f".data/discovery-{kind}.json").write_text(
                    json.dumps({"dom": dom.get("result", {}).get("value"), "responses": bodies}, ensure_ascii=False),
                    encoding="utf-8",
                )
                if kind == "products":
                    pager = await call(
                        "Runtime.evaluate",
                        {
                            "expression": "[...document.querySelectorAll('[data-testid]')].filter(e=>/pagination|pager/i.test(e.getAttribute('data-testid'))).map(e=>e.outerHTML.slice(0,1800)).slice(0,20)",
                            "returnByValue": True,
                        },
                    )
                    Path(".data/products-pager.json").write_text(
                        json.dumps(pager, ensure_ascii=False), encoding="utf-8"
                    )
                print(kind, "saved", len(bodies), "responses")
            finally:
                reader.cancel()
        await client.get(f"http://127.0.0.1:9222/json/close/{target_id}")


async def main():
    for kind, url in URLS.items():
        if len(sys.argv) == 1 or kind in sys.argv[1:]:
            await inspect(kind, url)


asyncio.run(main())
