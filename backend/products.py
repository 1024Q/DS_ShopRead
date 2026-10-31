"""Resume the observed all-products query through the shared durable pager."""

import asyncio, json
from . import collection_checkpoint as checkpoint
from .business_reader import checked_result, normalize
from .sync_progress import report
from .read_errors import PageReadError
from .read_budget import current as read_budget
from .async_database import run as database_work


async def yield_saved(page):
    context = checkpoint.current.get()
    report(page, stage="商品读取预算不足，保留分页断点等待续采")
    if context:
        await database_work(context.save)
        raise checkpoint.YieldCollection()
    raise PageReadError("商品读取预算不足，当前读取未完成，请稍后重试")


async def ensure_budget(page):
    budget = read_budget.get()
    if budget and budget.remaining() <= min(2, budget.seconds / 10):
        await yield_saved(page)


async def step(page, operation, stage, seconds=20):
    """Keep time for a durable yield instead of letting the task timer cancel it."""
    await ensure_budget(page)
    budget = read_budget.get()
    available = seconds if budget is None else min(seconds, budget.remaining() - min(2, budget.seconds / 10))
    limited = available < seconds
    report(page, stage=stage)
    try:
        async with asyncio.timeout(available):
            return await operation()
    except TimeoutError:
        if limited:
            await yield_saved(page)
        raise PageReadError(stage + "超时；保留分页断点，按计划重试") from None


async def collect(page):
    checked_result(await step(page, page.products.get, "等待商品初始列表响应"))
    # The response can precede React rendering. Wait for the actual control,
    # instead of always sleeping and then failing on a not-yet-rendered list.
    selector = "const e=[...document.querySelectorAll('span,a,div,button')].find(e=>e.children.length===0&&e.textContent.trim()==='全部'&&e.offsetWidth>0);"
    for _ in range(20):
        ready = await step(
            page, lambda: page.evaluate("(()=>{" + selector + "return !!e;})()"), "等待全部商品筛选控件就绪"
        )
        if ready:
            break
        await asyncio.sleep(0.2)
    else:
        raise PageReadError("商品列表已响应，但全部商品筛选控件尚未就绪")
    if callable(getattr(page, "reset_products", None)):
        page.reset_products()
    else:
        while not page.products.empty():
            page.products.get_nowait()
    clicked = await step(
        page, lambda: page.evaluate("(()=>{" + selector + "if(e)e.click();return !!e;})()"), "切换全部商品查询"
    )
    if not clicked:
        raise PageReadError("全部商品查询控件已变化，保留断点后重试")
    response = await step(page, page.products.get, "等待全部商品第一页响应")
    checked_result(response)
    body = response.get("_request", {})
    fields = [k for k in ("page", "pageNo", "pageNum", "pageNumber", "page_num", "page_number") if k in body]
    if len(fields) != 1:
        raise ValueError("商品分页请求字段发生变化，请更新读取适配")
    field = fields[0]
    if int(body[field]) != 1:
        raise ValueError("商品查询筛选状态不一致：全部商品未回到第一页")

    initial = response
    pagination = None

    async def fetch(number):
        nonlocal initial, pagination
        if number == 1 and initial is not None:
            response = initial
            initial = None
        else:
            # The all-products response can arrive while React still holds the
            # previous filter's callback. Wait for props matching the last verified
            # response before invoking the authenticated native transport.
            script = """(()=>{const expected=EXPECTED;
            const e=document.querySelector('[data-testid=beast-core-pagination]');if(!e||e.offsetWidth===0)return false;
            let f=e[Object.keys(e).find(k=>k.startsWith('__reactFiber$')||k.startsWith('__reactInternalInstance$'))];
            for(let i=0;f&&i<8;i++,f=f.return){const p=f.memoizedProps||{};
              if(typeof p.onChange==='function'&&typeof p.current==='number'&&typeof p.pageSize==='number'&&typeof p.total==='number'){
                if(p.total!==expected.total||p.current!==expected.current||
                   (expected.pageSize!==null&&p.pageSize!==expected.pageSize)||p.pageSize<=0||
                   NUMBER<1||NUMBER>Math.max(1,Math.ceil(p.total/p.pageSize)))return false;
                p.onChange(NUMBER,p.pageSize);return true;}}
            return false;})()""".replace("EXPECTED", json.dumps(pagination)).replace("NUMBER", str(int(number)))
            for _ in range(20):
                if await step(page, lambda: page.evaluate(script), f"等待商品第 {number} 页原生分页控件"):
                    break
                await asyncio.sleep(0.2)
            else:
                raise PageReadError("商品分页控件尚未与已读取列表同步；保留分页断点，按计划重试")

            async def response_for_page():
                while True:
                    value = await page.products.get()
                    # HTTP/authentication failures may have no request body.
                    # Report them now instead of waiting until the timeout.
                    checked_result(value)
                    observed = value.get("_request", {})
                    if observed.get(field) == number and all(
                        observed.get(k) == body.get(k)
                        for k in ("is_onsale", "sold_out", "pre_sale_type", "shipment_time_type", "size")
                    ):
                        return value

            response = await step(page, response_for_page, f"等待商品第 {number} 页列表响应")
        data = checked_result(response)
        if not isinstance(data, dict) or not isinstance(data.get("goods_list"), list) or data.get("total") is None:
            raise ValueError("商品列表字段发生变化，请更新读取适配")
        rows = [normalize("products", row) for row in data["goods_list"]]
        total = int(data["total"])
        size = response.get("_request", {}).get("size")
        pagination = {"current": int(number), "total": total, "pageSize": int(size) if size is not None else None}
        return {"rows": rows, "ids": [r["id"] for r in rows], "total": total}

    key = "products:all:v1"  # Request anti-bot tokens are ephemeral, never checkpoint identity.
    progress = lambda n, total: report(page, n, total, "分批读取商品列表")
    if checkpoint.current.get():
        return await checkpoint.paginate(key, fetch, progress)
    # Manual reads without a durable task remain bounded and explicitly partial.
    rows = {}
    number = 1
    while True:
        batch = await fetch(number)
        previous = len(rows)
        rows.update((r["id"], r) for r in batch["rows"])
        progress(len(rows), batch["total"])
        done = len(rows) >= batch["total"]
        if done or len(rows) >= 2000 or len(rows) == previous:
            return {"rows": list(rows.values()), "source_total": batch["total"], "complete": done}
        number += 1
