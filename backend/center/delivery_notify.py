"""Local wake-up hint only; the durable outbox scan remains authoritative.

A hint may name the shop that published, so only that shop's upload lane is
woken instead of rescanning every shop database.
"""

import asyncio, json, socket
from pathlib import Path


def notify(directory, shop_id=None):
    try:
        port = int(json.loads((Path(directory) / "delivery-notify.local.json").read_text("utf-8"))["port"])
        message = b"publish" if shop_id is None else b"publish:" + str(int(shop_id)).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(message, ("127.0.0.1", port))
    except OSError, ValueError, KeyError, TypeError:
        pass


def deliver(targets, data):
    if data == b"publish":
        shop = None
    elif data.startswith(b"publish:") and data[8:].isdigit():
        shop = data[8:].decode()
    else:
        return
    for target in targets:
        if hasattr(target, "notify"):
            target.notify(shop)  # per-shop groups wake only that shop
        elif hasattr(target, "wake"):
            target.wake.set()
        else:
            target.set()


async def listen(directory, events):
    class Wake(asyncio.DatagramProtocol):
        def datagram_received(self, data, address):
            deliver(events, data)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Wake, local_addr=("127.0.0.1", 0))
    try:
        from ..collector_update import write_json

        write_json(Path(directory) / "delivery-notify.local.json", {"port": transport.get_extra_info("sockname")[1]})
    except Exception:
        transport.close()
        raise
    return transport
