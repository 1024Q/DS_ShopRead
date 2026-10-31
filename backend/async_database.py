"""Finish database work before cancellation releases a collection's shop lock."""

from . import recovery_log
import asyncio
import contextvars
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext


def _workers(name, default):
    try:
        return max(2, min(64, int(os.environ.get(name, default))))
    except ValueError:
        return default


# Write admission may wait on another process. Keep these waits away from both
# read-only database work and asyncio's shared DNS/filesystem worker pool.
_writes = ThreadPoolExecutor(_workers("ECMP_DB_WRITE_THREADS", 8), thread_name_prefix="db-write")
_reads = ThreadPoolExecutor(_workers("ECMP_DB_READ_THREADS", 4), thread_name_prefix="db-read")
_financial_files = ThreadPoolExecutor(2, thread_name_prefix='refund-file')


async def run(operation, *args, **kwargs):
    return await _run(_writes, operation, *args, **kwargs)


async def read(operation, *args, **kwargs):
    """Execute a known read-only operation independently of waiting writers."""
    return await _run(_reads, operation, *args, **kwargs)


async def financial_file(operation, *args, **kwargs):
    """Durable financial files must not wait behind busy SQLite writers."""
    return await _run(_financial_files, operation, *args, **kwargs)


async def _run(executor, operation, *args, **kwargs):
    from .database_writer import operation as context

    label = context.get()
    token = context.set(label + " / " + getattr(operation, "__qualname__", "入库"))
    try:
        call = functools.partial(operation, *args, **kwargs)
        task = asyncio.get_running_loop().run_in_executor(executor, contextvars.copy_context().run, call)
    finally:
        context.reset(token)
    from .read_budget import current

    budget = current.get()
    from .node_dispatch import current_execution
    execution = current_execution.get() if executor is _writes else None
    suspended = False
    if execution:
        await execution.database()
        suspended = True
    from .collection_metrics import phase
    with phase('database' if executor is _writes else 'local_read', getattr(operation, '__qualname__', '数据库处理')) if executor is not _financial_files else nullcontext(), budget.database() if budget else nullcontext():
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A second stop/pause request must not cancel the waiter while the
            # native database thread still holds its transaction. Shield every
            # wait, including cancellation cleanup, before releasing shop locks.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                try:
                    task.result()
                except Exception:
                    recovery_log.suppressed("async_database.py:30 恢复步骤未完成", warning=False)
            raise
    # Resume only after a successful, fully drained durable batch. Failure and
    # cancellation leave the business lease held for worker cleanup, never
    # creating a replacement while the old native transaction is alive.
    if suspended:
        await execution.resume()
    return result


async def record_state(operation, *args, **kwargs):
    """Retry idempotent error/lease metadata only; never replay business publication."""
    import sqlite3, logging

    for attempt in range(3):
        try:
            await run(operation, *args, **kwargs)
            return True
        except sqlite3.OperationalError as error:
            if not any(word in str(error).lower() for word in ("locked", "busy")):
                raise
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
            else:
                logging.getLogger(__name__).error(
                    "任务状态暂未写入数据库（写锁繁忙），保留上次成功数据；任务释放后将重新调度"
                )
                return False
