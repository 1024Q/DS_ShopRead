"""Fence network observations when an owned target changes reader adapters."""

from urllib.parse import urlsplit


class DocumentGuard:
    def __init__(self):
        self.frame = None
        self.loader = None
        self.requests = set()
        self.started = False
        self.url = None
        self.previous_loader = None

    def navigate(self, url=None):
        self.started = True
        self.previous_loader = self.loader
        self.url = url or self.url
        self.frame = self.loader = None
        self.requests.clear()

    def accepts(self, message):
        method = message.get("method", "")
        params = message.get("params", {})
        if method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if frame.get("parentId"):
                return True
            self.frame = self.loader = None
            self.requests.clear()
            expected, actual = urlsplit(self.url or ""), urlsplit(frame.get("url", ""))
            if (
                self.started
                and not frame.get("parentId")
                and frame.get("loaderId") != self.previous_loader
                and (actual.hostname, actual.path.rstrip("/")) == (expected.hostname, expected.path.rstrip("/"))
            ):
                self.frame, self.loader = frame.get("id"), frame.get("loaderId")
                self.requests.clear()
            return True
        if not method.startswith("Network."):
            return True
        rid = params.get("requestId")
        if method == "Network.requestWillBeSent":
            valid = bool(self.loader and params.get("loaderId") == self.loader and params.get("frameId") == self.frame)
            if valid:
                self.requests.add(rid)
            else:
                self.requests.discard(rid)
            return valid
        valid = rid in self.requests
        if method in ("Network.loadingFinished", "Network.loadingFailed"):
            self.requests.discard(rid)
        return valid
