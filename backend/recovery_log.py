"""Rate-limited diagnostics for recoverable failures, without exception secrets."""

import logging
import sys
import threading
import time
import traceback
from pathlib import Path

_lock = threading.Lock()
_last = {}


def suppressed(context, *, warning=False):
    error = sys.exc_info()[1]
    if error is None:
        return
    if type(error).__name__ == "ConnectionClosedOK" or (
        type(error).__name__ == "WebSocketDisconnect" and getattr(error, "code", None) in (1000, 1001)
    ):
        warning = False
    now = time.monotonic()
    with _lock:
        if now - _last.get(context, -float("inf")) < 60:
            return
        if len(_last) >= 256:
            _last.clear()
        _last[context] = now
    # Error messages/locals can include URLs, credentials, or uploaded contents.
    frames = traceback.extract_tb(error.__traceback__)[-4:]
    location = " → ".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames)
    logging.getLogger("ecmp.recovery").log(
        logging.WARNING if warning else logging.DEBUG,
        "%s（%s）· %s；继续既有恢复流程",
        context,
        type(error).__name__,
        location,
    )
