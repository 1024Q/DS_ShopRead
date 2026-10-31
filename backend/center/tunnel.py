"""Authenticated outbound node tunnel. Mutations and browser input are never replayed."""

import logging
from .. import recovery_log
import asyncio
import base64
import json
import uuid
import contextlib
from collections import deque
from dataclasses import dataclass, field
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from .tunnel_protocol import REQUEST_LIMIT, CONTROL_LIMIT
from .download_stream import DownloadStream

MAX_BODY = 32 * 1024 * 1024
CAPABILITIES = {"binary-body", "multipart-upload", "chunked-body", "job-notify"}
# Live tunnels re-check their node credential so a revocation handled by another worker still cuts them off.
RECHECK = 30


def content(result):
    return result["content"] if "content" in result else base64.b64decode(result.get("body", ""))


class ViewerRelay:
    """A slow viewer cannot block the node tunnel. Retain only its newest frame."""

    def __init__(self, socket):
        self.socket = socket
        self.items = deque()
        self.changed = asyncio.Event()
        self.task = asyncio.create_task(self.run())

    def offer(self, data, binary=False):
        frame = binary
        if not binary:
            with contextlib.suppress(ValueError, TypeError):
                frame = json.loads(data).get("type") == "frame"
        if frame:
            self.items = deque(item for item in self.items if not item[2])
        if len(self.items) >= 32:
            raise RuntimeError("Viewer control queue overflow")
        self.items.append((data, binary, frame))
        self.changed.set()

    async def run(self):
        try:
            while True:
                await self.changed.wait()
                data, binary, _ = self.items.popleft()
                if not self.items:
                    self.changed.clear()
                await asyncio.wait_for(self.socket.send_bytes(data) if binary else self.socket.send_text(data), 5)
        except Exception:
            with contextlib.suppress(Exception):
                await self.socket.close(code=1011)

    async def close(self, code=1012):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.socket.close(code=code)


@dataclass
class Peer:
    socket: WebSocket
    pending: dict = field(default_factory=dict)
    viewers: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    capabilities: set = field(default_factory=set)
    partial: dict = field(default_factory=dict)
    downloads: dict = field(default_factory=dict)

    async def send(self, message, data=None):
        async with self.lock:
            await self.socket.send_json(message)
            if data is not None:
                await self.socket.send_bytes(data)


class Hub:
    def __init__(self):
        self.peers = {}
        self.control_peers = {}
        self.recheck = RECHECK
        self.job_changes = None

    async def revoke(self, node):
        for peers in (self.peers, self.control_peers):
            peer = peers.pop(node, None)
            if peer:
                with contextlib.suppress(Exception):
                    await peer.socket.close(code=4401)

    async def watch(self, socket, check):
        while True:
            await asyncio.sleep(self.recheck)
            try:
                allowed = await asyncio.to_thread(check)
            except Exception:
                logging.getLogger(__name__).debug("节点隧道身份复核失败，保留连接等待下次复核", exc_info=True)
                continue
            if not allowed:
                with contextlib.suppress(Exception):
                    await socket.close(code=4401)
                return

    async def deliver(self, peer, ident, data, binary=False):
        viewer = peer.viewers.get(ident)
        if not viewer:
            return
        try:
            viewer.offer(data, binary)
        except RuntimeError, TimeoutError, WebSocketDisconnect:
            peer.viewers.pop(ident, None)
            try:
                await viewer.close(code=1011)
            except Exception:
                recovery_log.suppressed("center/tunnel.py:90 恢复步骤未完成", warning=True)
            await peer.send({"type": "close", "id": ident})

    async def notify_jobs(self, node, peer):
        signal = self.job_changes
        revision = -1
        while True:
            baseline = signal.revision()
            current = signal.revision(node)
            if revision != current:
                revision = current
                await peer.send({"type": "job_available"})
            await signal.wait(baseline, 30)

    async def attach(self, node, socket, control=False, check=None):
        await socket.accept()
        peers = self.control_peers if control else self.peers
        old = peers.get(node)
        if old:
            await old.socket.close(code=1012)
        offered = CAPABILITIES & set(socket.headers.get("x-tunnel-capabilities", "").replace(" ", "").split(","))
        if offered:
            await socket.send_json({"type": "hello", "capabilities": sorted(offered)})
        peer = Peer(socket, capabilities=offered)
        peers[node] = peer
        watcher = asyncio.create_task(self.watch(socket, check)) if check else None
        notifier = (
            asyncio.create_task(self.notify_jobs(node, peer))
            if self.job_changes and not control and "job-notify" in offered
            else None
        )
        try:
            while True:
                wire = await socket.receive()
                if wire["type"] == "websocket.disconnect":
                    break
                if wire.get("bytes") is not None:
                    packet = wire["bytes"]
                    if len(packet) < 16:
                        continue
                    stream = uuid.UUID(bytes=packet[:16]).hex
                    download = peer.downloads.get(stream)
                    if download:
                        download.offer(packet[16:])
                        continue
                    message = peer.partial.pop(stream, None)
                    if message:
                        future = peer.pending.get(stream)
                        if future and not future.done():
                            future.set_result({**message, "content": packet[16:]})
                        continue
                    await self.deliver(peer, stream, packet[16:], True)
                    continue
                message = json.loads(wire.get("text") or "{}")
                ident = message.get("id")
                if message.get("type") == "response" and message.get("chunked"):
                    future = peer.pending.get(ident)
                    if future and not future.done():
                        download = DownloadStream(peer, ident)
                        peer.downloads[ident] = download
                        future.set_result({**message, "stream": download.iterate()})
                elif message.get("type") in ("download_end", "download_error"):
                    download = peer.downloads.get(ident)
                    if download:
                        download.finish(
                            ConnectionError("节点下载失败") if message["type"] == "download_error" else None
                        )
                elif message.get("type") == "response" and message.get("binary"):
                    if ident in peer.pending:
                        peer.partial[ident] = message
                elif message.get("type") == "response":
                    future = peer.pending.get(ident)
                    if future and not future.done():
                        future.set_result(message)
                elif message.get("type") == "stream":
                    await self.deliver(peer, ident, message["data"])
                elif message.get("type") == "closed":
                    viewer = peer.viewers.pop(ident, None)
                    if viewer:
                        try:
                            await viewer.close(code=1011)
                        except Exception:
                            recovery_log.suppressed("center/tunnel.py:131 恢复步骤未完成", warning=True)
        finally:
            if notifier:
                notifier.cancel()
                await asyncio.gather(notifier, return_exceptions=True)
            if watcher:
                watcher.cancel()
            if peers.get(node) is peer:
                peers.pop(node, None)
            for download in list(peer.downloads.values()):
                download.finish(ConnectionError("节点下载连接中断"))
            for future in peer.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("节点连接中断；操作结果可能尚未确认，请核对后重试"))
            for viewer in list(peer.viewers.values()):
                try:
                    await viewer.close(code=1012)
                except Exception:
                    recovery_log.suppressed("center/tunnel.py:139 恢复步骤未完成", warning=True)

    async def request(self, node, method, path, query="", body=b"", content_type="application/json", stream=False):
        peer = self.peers.get(node)
        if not peer:
            raise HTTPException(503, "采集节点离线；已存数据仍可查看")
        if len(body) > MAX_BODY:
            raise HTTPException(413, "本批文件过大，请分批上传（每批不超过 32 MiB）")
        if content_type.startswith("multipart/form-data") and "multipart-upload" not in peer.capabilities:
            # Older collectors still accept the original PDF JSON contract.
            from ..file_upload import multipart

            try:
                name, data, fields = multipart(content_type, body, 5 * 1024 * 1024)
                if fields:
                    raise ValueError("上传字段无效")
            except ValueError:
                raise HTTPException(400, "上传格式无效") from None
            body = json.dumps({"name": name, "content": base64.b64encode(data).decode()}).encode()
            content_type = "application/json"
        if len(peer.pending) + len(peer.downloads) >= REQUEST_LIMIT:
            raise HTTPException(429, "节点操作繁忙，请稍后重试")
        ident = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        peer.pending[ident] = future
        try:
            message = {"type": "request", "id": ident, "method": method, "path": path, "query": query}
            if stream and "chunked-body" in peer.capabilities:
                message["stream"] = True
            if content_type != "application/json":
                message["content_type"] = content_type
            if body and "binary-body" in peer.capabilities:
                await peer.send({**message, "binary": True}, uuid.UUID(hex=ident).bytes + body)
            else:
                await peer.send({**message, "body": base64.b64encode(body).decode()})
            return await asyncio.wait_for(future, 55)
        except TimeoutError, ConnectionError, RuntimeError, WebSocketDisconnect:
            raise HTTPException(504, "节点操作未确认，请先核对执行结果；系统不会自动重放此操作") from None
        finally:
            peer.pending.pop(ident, None)
            peer.partial.pop(ident, None)

    async def control(self, node, shop, socket):
        peer = self.control_peers.get(node) or self.peers.get(node)
        await socket.accept()
        if not peer:
            await socket.close(code=1013)
            return
        if len(peer.viewers) >= CONTROL_LIMIT:
            await socket.close(code=1013)
            return
        ident = uuid.uuid4().hex
        relay = ViewerRelay(socket)
        peer.viewers[ident] = relay
        try:
            await socket.send_json({"type": "transport", "dedicated": self.control_peers.get(node) is peer})
            await peer.send({"type": "open", "id": ident, "shop": shop})
            while True:
                data = await socket.receive_text()
                if len(data) > 65536:
                    break
                await peer.send({"type": "input", "id": ident, "data": data})
        finally:
            peer.viewers.pop(ident, None)
            if self.peers.get(node) is peer or self.control_peers.get(node) is peer:
                # A separate task: the notice must reach the node even when this handler is being cancelled.
                notice = asyncio.create_task(peer.send({"type": "close", "id": ident}))
                notice.add_done_callback(lambda t: t.cancelled() or t.exception())
            await relay.close()
