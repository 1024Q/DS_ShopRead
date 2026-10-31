"""Shared collector URL policy: TLS for public endpoints, literal private IPs for LAN."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit

LAN_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def center_url(value):
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("中心地址格式无效，请填写中心 HTTPS 地址或局域网 IP")
    host = parsed.hostname.lower()
    local = host == "localhost"
    lan = False
    try:
        address = ipaddress.ip_address(host)
        local = address.is_loopback
        lan = any(address.version == network.version and address in network for network in LAN_NETWORKS)
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and (local or lan)):
        raise ValueError("公网中心必须使用 HTTPS；局域网 HTTP 请填写 10.x、172.16–31.x 或 192.168.x 的 IP 地址")
    # The existing local dashboard port is distinct from the collector API port.
    if parsed.scheme == "http" and (local or lan) and parsed.port == 3211:
        parsed = parsed._replace(netloc=("[" + host + "]" if ":" in host else host) + ":8010")
    return urlunsplit(parsed).rstrip("/")
