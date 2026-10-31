"""Status persistence must not terminate delivery or turn a center receipt into failure."""

import logging, time, threading
from ..collector_update import write_json

LOG = logging.getLogger("ecmp.agent")
_warnings = {}
_lock = threading.Lock()


def save_state(path, value):
    try:
        write_json(path, value)
    except OSError as error:
        key = str(path)
        now = time.monotonic()
        with _lock:
            warn = key not in _warnings or now - _warnings[key] >= 60
            if warn:
                _warnings[key] = now
        if warn:
            LOG.warning(
                "上传状态文件暂未保存：%s · %s；保留原文件，下一轮重试，中心已确认的数据不记为上传失败",
                path.name,
                error,
            )
        return False
    with _lock:
        recovered = _warnings.pop(str(path), None) is not None
    if recovered:
        LOG.info("上传状态文件已恢复保存：%s", path.name)
    return True
