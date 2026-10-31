"""Bounded authentication failure tracking for direct center access."""

import ipaddress
import logging
import math
import threading
import time
from fastapi import HTTPException

PEER_HEADER = "x-ecmp-request-peer"


def client_address(request):
    """Return (address, trusted) for rate limiting.

    The local Next.js server stamps the real browser address into PEER_HEADER and
    always overwrites any browser-supplied value. The header is honoured only when
    the TCP peer is loopback, so LAN clients cannot spoof another address.
    """
    host = request.client.host if request.client else "unknown"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        forwarded = request.headers.get(PEER_HEADER, "").strip()
        if forwarded and len(forwarded) <= 64:
            return forwarded, True
        # Local tooling without a stamped peer: never lock out valid sessions.
        return host, False
    return host, True


class LoginGuard:
    def __init__(self, clock=time.monotonic, limit=5, cooldown=900, capacity=4096):
        self.clock, self.limit, self.cooldown, self.capacity = clock, limit, cooldown, capacity
        self.failures = {}
        self.lock = threading.Lock()

    def _expire(self, now):
        for key, (_, expires) in list(self.failures.items()):
            if expires <= now:
                del self.failures[key]

    def _blocked(self, address, now):
        count, expires = self.failures.get(address, (0, now))
        return expires - now if count >= self.limit and expires > now else 0

    @staticmethod
    def _too_many(remaining):
        return HTTPException(
            429,
            "身份验证失败次数过多，请稍后重试",
            headers={"Retry-After": str(max(1, math.ceil(remaining)))},
        )

    def check(self, address):
        """Refuse every attempt, including a correct token, while the source is locked."""
        now = self.clock()
        with self.lock:
            remaining = self._blocked(address, now)
        if remaining:
            raise self._too_many(remaining)

    def reject(self, address):
        now = self.clock()
        with self.lock:
            self._expire(now)
            remaining = self._blocked(address, now)
            if remaining:
                # A locked source cannot extend its own lock by retrying.
                raise self._too_many(remaining)
            if address not in self.failures and len(self.failures) >= self.capacity:
                raise self._too_many(60)
            count, _ = self.failures.get(address, (0, now))
            self.failures[address] = (count + 1, now + self.cooldown)
        logging.getLogger("ecmp.center.security").warning("管理认证失败，来源 %s", address)
        raise HTTPException(401, "中心身份验证失败")

    def verified(self, address):
        # A correct token clears earlier mistakes, but never an active lock.
        now = self.clock()
        with self.lock:
            if not self._blocked(address, now):
                self.failures.pop(address, None)
