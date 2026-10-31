def report(target, loaded=0, total=None, stage="读取中"):
    session = getattr(target, "session", target)
    callback = getattr(session, "read_progress", None)
    if callback:
        callback(loaded, total, stage)
