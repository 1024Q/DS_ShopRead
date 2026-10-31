"""Only accept complete platform shop names; masked account labels are not identities."""

import logging
import json
import time


def complete(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "*＊●")


async def resolve(session, name):
    if complete(name):
        return name.strip()
    if time.monotonic() < getattr(session, "name_retry_at", 0):
        return ""
    session.name_retry_at = time.monotonic() + 300
    try:
        # Existing authenticated, read-only merchant identity endpoint, same as tax_info.
        raw = await session.evaluate(
            """(async()=>{if(location.hostname!=="mms.pinduoduo.com")return null;const r=await fetch("/earth/api/mallInfo/queryFinalCredentialNew",{credentials:"include",signal:AbortSignal.timeout(5000)});if(!r.ok)return null;return await r.text();})()"""
        )
        from .business_reader import checked_result

        data = checked_result(json.loads(raw)) if raw else {}
        candidate = (data.get("queryDetailResult") or {}).get("mallName", "")
        if complete(candidate):
            return candidate.strip()
    except Exception:
        logging.getLogger(__name__).debug("读取店铺名称失败，保留已保存名称", exc_info=True)
    # Preserve a trusted saved name; never guess the masked characters.
    for key in ("detected_name", "name"):
        candidate = session.shop.get(key, "")
        if complete(candidate):
            return candidate
    return ""
