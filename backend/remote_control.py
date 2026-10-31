"""Bounded, acknowledged browser frames and ordered input over a persistent CDP session."""

import base64, struct
import asyncio
import contextlib
import json
import time
from urllib.parse import urlsplit, urlunsplit
import websockets
from starlette.websockets import WebSocketDisconnect

PRESETS = {
    "smooth": {"quality": 45, "maxWidth": 1024, "maxHeight": 768, "fps": 8},
    "balanced": {"quality": 65, "maxWidth": 1440, "maxHeight": 1000, "fps": 12},
    "clear": {"quality": 85, "maxWidth": 1920, "maxHeight": 1200, "fps": 16},
}
KEYS = {
    "Backspace": 8,
    "Tab": 9,
    "Escape": 27,
    "Delete": 46,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Home": 36,
    "End": 35,
    "Enter": 13,
}


async def bridge(ws, url, token):
    parts = urlsplit(url)
    url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, parts.path, parts.query, ""))
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": "Bearer " + token},
            proxy=None,
            max_size=16 * 1024 * 1024,
            max_queue=1,
            open_timeout=8,
        ) as upstream:
            await ws.accept()

            async def incoming():
                while True:
                    message = await ws.receive_text()
                    if len(message) > 16384:
                        raise ValueError("input too large")
                    await upstream.send(message)

            async def outgoing():
                async for message in upstream:
                    await (ws.send_bytes(message) if isinstance(message, bytes) else ws.send_text(message))
                await ws.close(
                    code=upstream.close_code if upstream.close_code and 4000 <= upstream.close_code < 5000 else 1012
                )

            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except websockets.ConnectionClosed as error:
        with contextlib.suppress(Exception):
            await ws.close(code=error.code if 4000 <= error.code < 5000 else 1012)
    except Exception:
        with contextlib.suppress(Exception):
            if ws.application_state.name == "CONNECTING":
                await ws.accept()
            await ws.close(code=4406)


class CDP:
    def __init__(self, socket, on_frame):
        self.socket = socket
        self.on_frame = on_frame
        self.sequence = 0
        self.pending = {}

    async def call(self, method, params=None):
        self.sequence += 1
        seq = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[seq] = future
        try:
            await self.socket.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(seq, None)

    async def listen(self):
        try:
            async for data in self.socket:
                message = json.loads(data)
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ValueError("Browser command failed"))
                    else:
                        future.set_result(message.get("result", {}))
                elif message.get("method") == "Page.screencastFrame":
                    self.on_frame(message["params"])
                    self.sequence += 1
                    await self.socket.send(
                        json.dumps(
                            {
                                "id": self.sequence,
                                "method": "Page.screencastFrameAck",
                                "params": {"sessionId": message["params"]["sessionId"]},
                            }
                        )
                    )
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Browser disconnected"))


async def dispatch(cdp, event):
    kind = event.get("type")
    if kind == "text":
        value = event.get("text", "")
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError("text")
        await cdp.call("Input.insertText", {"text": value})
        return
    if kind in ("key", "enter"):
        key = "Enter" if kind == "enter" else event.get("key")
        if key not in KEYS:
            raise ValueError("key")
        for phase in ("keyDown", "keyUp"):
            await cdp.call("Input.dispatchKeyEvent", {"type": phase, "key": key, "windowsVirtualKeyCode": KEYS[key]})
        return
    if kind not in ("mouseMoved", "mousePressed", "mouseReleased", "wheel"):
        raise ValueError("type")

    def number(name, limit, lower=0):
        value = event.get(name, 0)
        if not isinstance(value, (int, float)) or not lower <= value <= limit:
            raise ValueError(name)
        return value

    params = {"type": "mouseWheel" if kind == "wheel" else kind, "x": number("x", 10000), "y": number("y", 10000)}
    if kind == "wheel":
        params.update(deltaX=number("delta_x", 2000, -2000), deltaY=number("delta_y", 2000, -2000))
    else:
        params.update(
            button="left" if kind != "mouseMoved" else "none",
            buttons=0
            if kind == "mouseReleased"
            else (int(bool(event.get("buttons", 0))) if kind == "mouseMoved" else 1),
            clickCount=0 if kind == "mouseMoved" else 1,
        )
    await cdp.call("Input.dispatchMouseEvent", params)


class Control:
    def __init__(self, ws, item, business):
        self.ws = ws
        self.item = item
        self.business = business
        self.latest = None
        self.changed = asyncio.Event()
        self.ack = asyncio.Event()
        self.ack.set()
        self.binary_frames = False
        self.paused = False
        self.preset = "balanced"
        self.configured = False
        self.mode = "stream"
        self.last_frame = 0
        self.started = time.monotonic()
        self.last_input = time.monotonic()
        self.sent_id = 0
        self.pending_id = None
        self.frame_window = 1
        self.inflight = {}
        self.last_sent = 0
        self.dropped = 0
        self.pressed = False
        self.position = {"x": 0, "y": 0}
        self.metrics = {"width": 1280, "height": 720}
        self.send_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.cdp = None
        self.last_action = 0
        self.repaired_action = 0

    async def send(self, message):
        async with self.send_lock:
            await asyncio.wait_for(self.ws.send_json(message), 10)

    def frame(self, params):
        meta = params.get("metadata", {})
        if meta.get("deviceWidth") and meta.get("deviceHeight"):
            self.metrics = {"width": meta["deviceWidth"], "height": meta["deviceHeight"]}
        if self.latest is not None:
            self.dropped += 1
        self.latest = params
        self.last_frame = time.monotonic()
        self.changed.set()

    async def configure(self, preset):
        if preset not in PRESETS:
            raise ValueError("preset")
        if self.configured and preset == self.preset:
            await self.send({"type": "mode", "mode": self.mode, "preset": preset})
            return
        self.preset = preset
        self.latest = None
        self.changed.clear()
        self.last_frame = 0
        self.started = time.monotonic()
        with contextlib.suppress(Exception):
            await self.cdp.call("Page.stopScreencast")
        try:
            await self.cdp.call(
                "Page.startScreencast",
                {"format": "jpeg", **{k: v for k, v in PRESETS[preset].items() if k != "fps"}, "everyNthFrame": 1},
            )
            self.mode = "stream"
        except Exception:
            self.mode = "snapshot"
        self.configured = True
        await self.send({"type": "mode", "mode": self.mode, "preset": preset})

    async def frames(self):
        while True:
            await self.changed.wait()
            await asyncio.wait_for(self.ack.wait(), 20)
            # Pace from the last send, not from the ACK: network RTT overlaps the
            # frame interval. New viewers negotiate two bounded in-flight frames.
            await asyncio.sleep(max(0, self.last_sent + 1 / PRESETS[self.preset]["fps"] - time.monotonic()))
            value = self.latest
            self.latest = None
            self.changed.clear()
            if not value or self.paused:
                continue
            meta = value.get("metadata", {})
            self.sent_id += 1
            self.pending_id = self.sent_id
            self.last_sent = time.monotonic()
            self.inflight[self.sent_id] = self.last_sent
            if len(self.inflight) >= self.frame_window:
                self.ack.clear()
            header = {
                "type": "frame",
                "id": self.sent_id,
                "width": meta.get("deviceWidth") or self.metrics["width"],
                "height": meta.get("deviceHeight") or self.metrics["height"],
                "queue_ms": round(max(0, self.last_sent - self.last_frame) * 1000),
                "dropped": self.dropped,
            }
            if self.binary_frames:
                encoded = json.dumps(header).encode()
                async with self.send_lock:
                    await asyncio.wait_for(
                        self.ws.send_bytes(struct.pack("!I", len(encoded)) + encoded + base64.b64decode(value["data"])),
                        10,
                    )
            else:
                await self.send({**header, "data": value["data"]})

    async def receive(self):
        # Input is acknowledged independently of rendering, and never waits for a frame ACK.
        while True:
            raw = await self.ws.receive_text()
            if len(raw) > 16384:
                raise ValueError("message too large")
            message = json.loads(raw)
            self.last_input = time.monotonic()
            if message.get("type") == "ack":
                ident = message.get("id")
                if isinstance(ident, int) and ident in self.inflight:
                    # Cumulative ACK allows relays/viewers to discard stale frames.
                    self.inflight = {key: value for key, value in self.inflight.items() if key > ident}
                    if len(self.inflight) < self.frame_window:
                        self.ack.set()
                    if not self.inflight:
                        self.pending_id = None
            elif message.get("type") == "ping":
                await self.send({"type": "pong", "at": message.get("at")})
            elif message.get("type") == "quality":
                self.binary_frames = bool(message.get("binary_frames", self.binary_frames))
                self.paused = bool(message.get("paused", False))
                self.frame_window = 2 if message.get("frame_window") == 2 else 1
                if self.paused:
                    self.pending_id = None
                    self.inflight.clear()
                    self.ack.set()
                elif len(self.inflight) < self.frame_window:
                    self.ack.set()
                async with self.command_lock:
                    await self.configure(message.get("preset"))
            elif message.get("type") == "input":
                events = message.get("events", [])
                if not isinstance(events, list) or not 1 <= len(events) <= 16:
                    raise ValueError("events")
                async with self.command_lock:
                    async with self.item.lock:
                        for event in events:
                            await dispatch(self.cdp, event)
                            if event.get("type") in ("mouseMoved", "mousePressed", "mouseReleased"):
                                self.position = {key: event.get(key, 0) for key in ("x", "y")}
                                self.pressed = (
                                    bool(event.get("buttons", 0))
                                    if event["type"] == "mouseMoved"
                                    else event["type"] == "mousePressed"
                                )
                await self.send({"type": "input_ack", "id": message.get("id")})
                self.last_action = time.monotonic()
            else:
                raise ValueError("message type")

    async def heartbeat(self):
        last_probe = 0
        while True:
            await asyncio.sleep(0.2)
            if self.item.retired or time.monotonic() - self.last_input > 30:
                raise ConnectionError("Control expired")
            self.item.control_until = time.time() + 10
            # Detect closed tabs/navigation to a newly selected platform page.
            if time.monotonic() - last_probe > 5:
                last_probe = time.monotonic()
                target = await self.item.target()
                if target["id"] != self.target_id:
                    raise ConnectionError("Page changed")
            if self.mode == "stream" and not self.last_frame and time.monotonic() - self.started > 3:
                self.mode = "snapshot"
                with contextlib.suppress(Exception):
                    await self.cdp.call("Page.stopScreencast")
                await self.send({"type": "mode", "mode": "snapshot", "preset": self.preset})
            # Hidden/background pages may deliver an initial frame then stop issuing
            # damage events. Repair once after input, without showing the window.
            repair = (
                self.last_action > self.repaired_action
                and self.last_frame < self.last_action
                and time.monotonic() - self.last_action > 0.2
            )
            if not self.paused and (self.mode == "snapshot" or repair) and self.ack.is_set():
                value = await self.cdp.call(
                    "Page.captureScreenshot", {"format": "jpeg", "quality": PRESETS[self.preset]["quality"]}
                )
                self.frame({"data": value["data"]})
                self.repaired_action = self.last_action

    async def run(self):
        tasks = []
        self.item.control_until = time.time() + 30
        try:
            await self.business.cancel_shop(self.item.shop["id"])
            target = await self.item.target()
            self.target_id = target["id"]
            async with websockets.connect(
                target["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024, max_queue=1, proxy=None
            ) as socket:
                self.cdp = CDP(socket, self.frame)
                listener = asyncio.create_task(self.cdp.listen())
                tasks.append(listener)
                try:
                    metrics = await self.cdp.call(
                        "Runtime.evaluate",
                        {"expression": "({width:innerWidth,height:innerHeight})", "returnByValue": True},
                    )
                    viewport = metrics.get("result", {}).get("value", {})
                    self.metrics = {"width": viewport.get("width", 1280), "height": viewport.get("height", 720)}
                    await self.configure(self.preset)
                    tasks.extend(asyncio.create_task(job()) for job in (self.receive, self.frames, self.heartbeat))
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    # Release a drag if the viewer disappears or connection drops.
                    with contextlib.suppress(Exception):
                        if self.pressed:
                            await asyncio.wait_for(dispatch(self.cdp, {"type": "mouseReleased", **self.position}), 2)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.cdp.call("Page.stopScreencast"), 2)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.item.control_until = 0
            self.business.wake.set()


async def serve(ws, item, business):
    await ws.accept()
    if getattr(item, "remote_control_active", False):
        await ws.close(code=4409)
        return
    item.remote_control_active = True
    try:
        await Control(ws, item, business).run()
    except WebSocketDisconnect, websockets.ConnectionClosed:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close(code=1012)
    finally:
        item.remote_control_active = False
