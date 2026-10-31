"""Browser resource readiness; admission belongs only to node_dispatch."""



def browser_blocked(item):
    recovery = getattr(item, "collection_recovery_task", None)
    if recovery is not None and not recovery.done() and getattr(item, "collection_recovery_exclusive", False):
        return True
    lock = item.lock
    return bool(lock.writer or lock.waiting) if hasattr(lock, "read") else lock.locked()
