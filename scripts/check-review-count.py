import asyncio, json
from backend.store import Store
from backend.browser import Session
from backend.business import options_for


async def main():
    store = Store()
    session = Session(store.list()[0], store)
    options = options_for("reviews", stars="1,2,3", reply="unreplied")
    for page in (1, 2):
        body = {
            "startTime": options["start"],
            "endTime": options["end"],
            "pageNo": page,
            "pageSize": 10,
            "descScore": ["1", "2", "3"],
            "replyStatus": "2",
            "orderSn": "",
        }
        raw = await session.evaluate(
            '(async()=>await (await fetch("/saturn/reviews/list",{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify('
            + json.dumps(body)
            + ")})).text())()"
        )
        data = json.loads(raw)
        print(
            page,
            {k: v for k, v in (data.get("result") or {}).items() if k != "data"},
            "rows",
            len((data.get("result") or {}).get("data", [])),
        )


asyncio.run(main())
