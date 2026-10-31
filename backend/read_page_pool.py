"""Small session-owned idle page cache; never share accounts or active readers."""

from . import recovery_log
import asyncio, logging, time
from contextlib import asynccontextmanager
from .request_reuse import owner
from .sync_progress import report

TTL = 90


async def close_idle(session, fence=True):
    """Close idle work tabs.

    fence=True is only for whole-browser events (stop, sleep, restart, logout,
    login recovery): advancing the epoch makes every active reader discard its
    tab instead of returning it. Ordinary page creation uses release_idle(),
    which never touches tabs other tasks are currently using.
    """
    root = owner(session)
    epoch = getattr(root, "_read_page_pool_epoch", 0)
    if fence:
        # Advancing before awaiting also fences readers that finish while idle
        # pages are being closed (including service stop / browser recovery).
        epoch += 1
        root._read_page_pool_epoch = epoch
    pool = getattr(root, "_idle_read_pages", {})
    entries = list(pool.values())
    pool.clear()
    for page, stamp, identity, timer in entries:
        timer.cancel()
    results = await asyncio.gather(*(page.__aexit__() for page, _, _, _ in entries), return_exceptions=True)
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logging.getLogger(__name__).debug(
                "闲置采集页已失联，继续回收其他页面", exc_info=(type(result), result, result.__traceback__)
            )
        elif isinstance(result, BaseException):
            raise result
    return epoch


async def release_idle(session):
    """Free idle tabs before an exclusive direct reader; active tabs stay valid."""
    return await close_idle(session, fence=False)


def identity(root):
    return (
        id(getattr(root, "process", None)),
        getattr(root, "port", None),
        root.shop.get("profile_version", 0),
        getattr(root, "state", None),
        getattr(root, "manual_visibility", None) is True,
    )


@asynccontextmanager
async def borrow(session, kind, factory):
    root = owner(session)
    enabled = getattr(root, "shop", {}).get("platform", "pdd") == "pdd" and getattr(root, "state", None) == "logged_in"
    if not enabled:
        async with factory(session) as page:
            yield page
        return
    pool = getattr(root, "_idle_read_pages", None)
    if pool is None:
        pool = {}
        root._idle_read_pages = pool
    page = None
    idle_ttl = min(TTL, max(1, getattr(root, "read_page_idle_seconds", TTL)))
    generation = identity(root)
    epoch = getattr(root, "_read_page_pool_epoch", 0)
    while pool and page is None and epoch == getattr(root, "_read_page_pool_epoch", 0):
        # Each idle value is one owned tab, not one module. Remove it before
        # any await so another task can never lease the same tab.
        key = next(
            (
                key
                for key, (candidate, _, _, _) in pool.items()
                if getattr(candidate, "_pool_factory", None) is factory
                and getattr(candidate, "_pool_kind", None) == kind
            ),
            next(iter(pool)),
        )
        entry = pool.pop(key)
        report(session, stage="检查复用采集页面是否可用")
        candidate, stamp, previous, timer = entry
        timer.cancel()
        try:
            if (
                generation == previous
                and time.monotonic() - stamp < idle_ttl
                and not candidate.pending
                and not candidate.body_tasks
            ):
                candidate.session = session
                from .business_reader import ReadPage

                previous_kind = getattr(candidate, "_pool_kind", kind)
                same_reader = getattr(candidate, "_pool_factory", None) is factory and previous_kind == kind
                ready_expression = getattr(candidate, "reuse_ready_expression", None)
                expression = (
                    ready_expression(previous_kind)
                    if callable(ready_expression)
                    else ReadPage.reuse_ready_expression(candidate, previous_kind)
                )
                ready = await asyncio.wait_for(candidate.evaluate(expression), 3)
                if ready and not candidate.pending and not candidate.body_tasks:
                    if same_reader:
                        page = candidate
                        page.reused_kind = kind
                        if callable(getattr(page, "reset_buffers", None)):
                            page.reset_buffers()
                    elif callable(getattr(candidate, "rebind", None)):
                        report(session, stage="复用本店工作标签，切换采集模块")
                        page = await candidate.rebind(session, factory)
                        # The rebound tab still shows the previous module; open()
                        # may skip the full load for verified in-page reads.
                        page.soft_switch = True
                        page.soft_from_kind = previous_kind
        except asyncio.CancelledError:
            await candidate.__aexit__()
            raise
        except Exception:
            recovery_log.suppressed("read_page_pool.py:59 恢复步骤未完成", warning=False)
        if page is None:
            await candidate.__aexit__()
    if page is None:
        from .collection_metrics import count
        count('pages_created')
        if epoch != getattr(root, "_read_page_pool_epoch", 0) or generation != identity(root):
            from .read_errors import PageReadError

            raise PageReadError("店铺浏览器状态已变化，工作标签已回收")
        # No idle tab is usable. Allocate only for this real concurrent demand;
        # creating a sibling must not fence or close other active readers.
        report(session, stage="创建本店采集工作标签")
        candidate = factory(session)
        candidate._pooled_reader = True
        candidate._pool_epoch = epoch
        page = await candidate.__aenter__()
        epoch = getattr(page, "_pool_epoch", epoch)
    else:
        from .collection_metrics import count
        count('pages_reused')
    page._pool_factory = factory
    page._pool_kind = kind
    reusable = False
    try:
        if epoch != getattr(root, "_read_page_pool_epoch", 0) or generation != identity(root):
            from .read_errors import PageReadError

            raise PageReadError("店铺浏览器状态已变化，工作标签已回收")
        from .business_reader import foreground

        await foreground(page)
        yield page
        reusable = True
    except BaseException as error:
        from .collection_checkpoint import YieldCollection

        reusable = isinstance(error, YieldCollection)
        raise
    finally:
        if (
            reusable
            and epoch == getattr(root, "_read_page_pool_epoch", 0)
            and generation == identity(root)
            and not getattr(root, "retired", False)
            and not page.pending
            and not page.body_tasks
        ):
            if getattr(page, "soft_opened", False) and getattr(page, "soft_from_kind", None):
                # The tab still shows the earlier module's document; pool it under
                # that kind so the next readiness check matches the real page.
                page._pool_kind = page.soft_from_kind
            # Never leave stale response buffers available to the next scope.
            for name in ("order_responses", "products", "activities"):
                queue = getattr(page, name, None)
                if queue is not None:
                    while not queue.empty():
                        queue.get_nowait()
            key = id(page)

            def expire():
                current = pool.get(key)
                if current and current[0] is page:
                    pool.pop(key, None)
                    task = asyncio.create_task(page.__aexit__())
                    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

            timer = asyncio.get_running_loop().call_later(idle_ttl, expire)
            pool[key] = (page, time.monotonic(), generation, timer)
        else:
            await page.__aexit__()
