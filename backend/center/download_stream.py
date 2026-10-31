"""Credit-controlled tunnel downloads: at most one data chunk in flight."""

import asyncio
from .tunnel_protocol import CHUNK_BYTES


class DownloadStream:
    def __init__(self, peer, ident):
        self.peer, self.ident = peer, ident
        self.queue = asyncio.Queue(maxsize=2)
        self.closed = False

    def offer(self, chunk):
        if len(chunk) > CHUNK_BYTES or self.queue.full():
            raise ValueError("下载分块超出流量窗口")
        self.queue.put_nowait(chunk)

    def finish(self, error=None):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(error or None)

    async def iterate(self):
        try:
            while True:
                chunk = await asyncio.wait_for(self.queue.get(), 60)
                if chunk is None:
                    return
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk
                await self.peer.send({"type": "chunk_ack", "id": self.ident})
        finally:
            self.peer.downloads.pop(self.ident, None)
            await self.peer.send({"type": "close", "id": self.ident})
