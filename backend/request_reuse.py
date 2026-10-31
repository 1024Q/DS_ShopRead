"""Bounded, per browser-session cache for successful read-only merchant requests."""

import asyncio, contextvars, copy, json, time, math
from collections import OrderedDict
from contextlib import contextmanager, nullcontext

_bypass = contextvars.ContextVar("fresh_merchant_read", default=False)


@contextmanager
def fresh(enabled=True):
    token = _bypass.set(enabled or _bypass.get())
    try:
        yield
    finally:
        _bypass.reset(token)


def owner(session):
    while hasattr(session, "source") or hasattr(session, "session"):
        session = getattr(session, "source", None) or session.session
    return session


RATE_LIMIT_WORDS = ("操作太过频繁", "请求频繁", "请求过于频繁", "限流", "访问频繁")
PATH_COOLDOWN = 300  # same endpoint after the platform said "too frequent"
SHOP_COOLDOWN = 20  # every in-page read of that shop pauses briefly as well


class ShopReadCooldown(ValueError):
    cooldown_scope = "shop"

    def __init__(self, seconds):
        self.retry_after = seconds
        super().__init__(f"同店其他接口短暂冷却中（约 {int(seconds) + 1} 秒），保留断点后自动继续")


def check_cooldown(root, path):
    now = time.monotonic()
    left = cooling(root, path, now)
    if left <= 0:
        return
    if getattr(root, "_read_cooldowns", {}).get(path, 0) > now:
        raise ValueError(f"平台提示请求频繁，接口冷却中（约 {int(left) + 1} 秒），保留断点后自动继续")
    raise ShopReadCooldown(left)


def min_interval():
    import os

    try:
        value = float(os.environ.get("ECMP_READ_MIN_INTERVAL", "0.35"))
        return max(0.0, value) if math.isfinite(value) else 0.35
    except ValueError:
        return 0.35


def rate_limited(message):
    return any(word in str(message or "") for word in RATE_LIMIT_WORDS)


def cooling(root, path, now=None):
    """Seconds left before this endpoint may be requested again (0 when free)."""
    now = time.monotonic() if now is None else now
    paths = getattr(root, "_read_cooldowns", {})
    return max(0.0, paths.get(path, 0) - now, getattr(root, "_read_shop_cooldown", 0) - now)


def enter_cooldown(root, path, now=None):
    now = time.monotonic() if now is None else now
    paths = getattr(root, "_read_cooldowns", None)
    if paths is None:
        paths = {}
        root._read_cooldowns = paths
    paths[path] = max(paths.get(path, 0), now + PATH_COOLDOWN)
    root._read_shop_cooldown = max(getattr(root, "_read_shop_cooldown", 0), now + SHOP_COOLDOWN)


async def paced(root, path, fetch):
    """One request lane per shop: cooldown check, minimum spacing, then fetch.

    A rate-limit answer starts a cooldown so concurrent and retried reads stop
    immediately instead of each spending a collection slot on another refusal.
    """
    check_cooldown(root, path)
    lane = getattr(root, "_read_pace_lock", None)
    if lane is None:
        lane = asyncio.Lock()
        root._read_pace_lock = lane
    from .collection_metrics import phase, count
    with phase('request_wait', '等待同店接口最小间隔'):
        async with lane:
            wait = getattr(root, "_read_next_request", 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            # A preceding refusal may have started cooling while we waited.
            check_cooldown(root, path)
            root._read_next_request = time.monotonic() + min_interval()
    try:
        with phase('query', path):
            count('network_queries')
            result = await fetch()
    except Exception as error:
        if rate_limited(error):
            enter_cooldown(root, path)
        raise
    if isinstance(result, dict) and rate_limited(result.get("errorMsg") or result.get("error_msg")):
        enter_cooldown(root, path)
    return result


def identity(root):
    shop = getattr(root, 'shop', {})
    return (shop.get('id'), shop.get('platform', 'pdd'), shop.get('profile_version', 0),
            id(getattr(root, 'process', None)), getattr(root, 'port', None), getattr(root, 'state', None),
            getattr(root, '_read_cache_epoch', 0))


def invalidate(session):
    root = owner(session)
    root._read_cache_epoch = getattr(root, '_read_cache_epoch', 0) + 1
    for name in ('_merchant_reads', '_complete_reads'):
        getattr(root, name, {}).clear()


def readonly(path):
    from .operations import ENDPOINTS
    from .business_reader import READ_APIS
    return path in set(ENDPOINTS.values()) | set(READ_APIS.values())


def ttl():
    from .collection_metrics import current
    tracker = current.get()
    return min(30, tracker.meta.get('freshness_seconds', 30)) if tracker else 30


async def shared(root, attribute, key, fetch):
    """One producer; cancellation of a subscriber cannot cancel its siblings.

    A cancelled producer keeps its borrowed page alive until the dispatched read
    finishes. Its own caller still receives cancellation, never a false success.
    """
    from .collection_metrics import phase, count
    flights = getattr(root, attribute, None)
    if flights is None:
        flights = {}; setattr(root, attribute, flights)
    entry = flights.get(key)
    producer = entry is None
    if producer:
        task = asyncio.create_task(fetch())
        entry = {'task': task, 'subscribers': set()}
        flights[key] = entry
        def finished(done):
            if flights.get(key) is entry:
                flights.pop(key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    else:
        count('joined_queries')
    task = entry['task']
    subscriber = asyncio.current_task()
    entry['subscribers'].add(subscriber)
    try:
        with phase('shared_wait', '等待同店同参数只读查询完成') if not producer else nullcontext():
            return copy.deepcopy(await asyncio.shield(task))
    except asyncio.CancelledError:
        entry['subscribers'].discard(subscriber)
        if not entry['subscribers']:
            task.cancel()
        if producer:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        raise
    finally:
        entry['subscribers'].discard(subscriber)


def successful(result):
    return not (isinstance(result, dict) and (result.get('success') is False
                or rate_limited(result.get('errorMsg') or result.get('error_msg'))))


async def read(session, path, body, fetch):
    from .collection_metrics import count
    root = owner(session)
    if _bypass.get() or getattr(root, 'state', 'logged_in') != 'logged_in' or not readonly(path):
        return await paced(root, path, fetch)
    generation = identity(root)
    cache = getattr(root, '_merchant_reads', None)
    if cache is None or getattr(root, '_merchant_read_identity', None) != generation:
        cache = OrderedDict(); root._merchant_reads = cache; root._merchant_read_identity = generation
    key = json.dumps([path, body], sort_keys=True, separators=(',', ':'), allow_nan=False)
    now = time.monotonic()
    for old in list(cache):
        if now - cache[old][0] >= ttl():
            cache.pop(old, None)
    if key in cache:
        count('cache_hits'); cache.move_to_end(key)
        return copy.deepcopy(cache[key][1])
    async def produce():
        result = await paced(root, path, fetch)
        size = len(json.dumps(result, ensure_ascii=False).encode())
        if successful(result) and size <= 1024 * 1024 and identity(root) == generation:
            cache[key] = (time.monotonic(), copy.deepcopy(result), size)
            while len(cache) > 128 or sum(v[2] for v in cache.values()) > 8 * 1024 * 1024:
                cache.popitem(last=False)
        return result
    return await shared(root, '_merchant_flights', (generation, key), produce)


def scope_key(options):
    # Scope flags are part of the contract. Different ranges/statuses never
    # substitute for each other; identical low-level request bodies still join.
    return json.dumps(options, sort_keys=True, separators=(',', ':'), allow_nan=False)


def readonly_kind(kind):
    from .operations import ENDPOINTS
    from .business_reader import READ_APIS
    return kind in set(ENDPOINTS) | set(READ_APIS) | {'orders', 'products', 'goodsdata', 'ad_products', 'ad_reports', 'transactions', 'overview'}


def complete_cache(session):
    root = owner(session)
    if (_bypass.get() or not getattr(root, 'shop', None) or root.shop.get('platform', 'pdd') != 'pdd'
            or getattr(root, 'state', 'logged_in') != 'logged_in'):
        return None
    generation = identity(root)
    if getattr(root, '_complete_read_identity', None) != generation:
        root._complete_reads = OrderedDict(); root._complete_read_identity = generation
    cache = root._complete_reads
    now = time.monotonic()
    for key in list(cache):
        if now - cache[key][0] >= ttl():cache.pop(key, None)
    return cache


def lookup(session, kind, options):
    from .collection_metrics import count
    if not readonly_kind(kind):return None
    cache = complete_cache(session)
    if cache is None:return None
    key = json.dumps([kind, scope_key(options)])
    found = cache.get(key)
    if found:
        count('complete_hits'); cache.move_to_end(key)
        return copy.deepcopy(found[3])
    return None


async def complete(session, kind, options, fetch):
    if not readonly_kind(kind):return await fetch()
    found = lookup(session, kind, options)
    if found is not None:return found
    cache = complete_cache(session)
    if cache is None:return await fetch()
    root = owner(session); generation = identity(root)
    key = json.dumps([kind, scope_key(options)])
    async def produce():
        result = await fetch()
        if successful(result) and result.get('complete') is True and identity(root) == generation:
            size = len(json.dumps(result, ensure_ascii=False).encode())
            if size <= 4 * 1024 * 1024:
                cache[key] = (time.monotonic(), kind, dict(options), copy.deepcopy(result), size)
                while len(cache) > 64 or sum(v[4] for v in cache.values()) > 8 * 1024 * 1024:
                    cache.popitem(last=False)
        return result
    return await shared(root, '_complete_flights', (generation, key), produce)
