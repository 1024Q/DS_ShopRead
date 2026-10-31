"""Standard-library XML parsing that refuses DTDs and entity declarations.

Platform downloads (xlsx parts) never contain a DOCTYPE; refusing one blocks
entity-expansion attacks without adding a runtime dependency.
"""

import re
import xml.etree.ElementTree as ET

_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_MESSAGE = "表格文件包含 DTD 或实体声明，已拒绝解析"


def _check(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "ignore")
    if _DECLARATION.search(data):
        raise ValueError(_MESSAGE)


def fromstring(data):
    _check(data)
    return ET.fromstring(data)


class _Guarded:
    """File wrapper that scans each chunk (with overlap) before the parser sees it."""

    def __init__(self, source):
        self.source, self.tail = source, b""

    def read(self, size=-1):
        chunk = self.source.read(size)
        window = self.tail + chunk
        _check(window)
        self.tail = window[-16:]
        return chunk


def iterparse(source, events=("end",)):
    return ET.iterparse(_Guarded(source), events=events)
