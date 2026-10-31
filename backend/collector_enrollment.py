"""Persist a fresh per-installation secret before sending an approval request."""

import hashlib
import secrets
import socket
import httpx


def configuration(address, current, auto=False):
    if current and current.get("url") == address:
        return {**current, "auto": auto}
    return {
        "url": address,
        "token": secrets.token_urlsafe(32),
        "name": socket.gethostname(),
        "id": "",
        "enrollment": True,
        "auto": auto,
    }


def enroll(config, version=""):
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                config["url"] + "/agent/enroll",
                headers={"Authorization": "Bearer " + config["token"]},
                json={"name": config["name"], "version": version},
            )
    except httpx.HTTPError:
        raise ValueError("中心暂不可达，接入申请已保存在本机，将自动重试") from None
    if response.status_code == 404:
        raise ValueError("中心尚未支持机器审批，请先更新中心服务")
    if response.status_code != 200:
        raise ValueError("中心未受理申请，请检查地址或稍后重试")
    value = response.json()
    if value.get("state") not in ("approved", "pending", "rejected", "revoked", "expired"):
        raise ValueError("中心返回的申请状态无效")
    return value


def code(config):
    return hashlib.sha256(config["token"].encode()).hexdigest()[:12].upper()
