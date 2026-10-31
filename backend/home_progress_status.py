"""Coverage eligibility and waiting labels without changing scheduling policy."""


def waking(item):
    task = getattr(item, "collection_wake_task", None)
    return bool(
        getattr(item, "wanted_running", False)
        and not getattr(item, "retired", False)
        and item.state in ("sleeping", "starting", "logging_in")
        and task is not None
        and not task.done()
    )


def overdue_reason(status, progress, blocked=False):
    if status == "waking":
        return "waking"
    if progress.get("phase") == "running":
        return "saving" if progress.get("read_finished") else "running"
    if status in ("error", "retry_wait") or blocked:
        return "retry"
    if status == "sleeping":
        return "sleeping"
    return "queued" if status in ("queued", "deferred") else "waiting"
