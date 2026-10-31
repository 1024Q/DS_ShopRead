"""Module capabilities shared with the frontend registry."""

import json
from pathlib import Path

REGISTRY = json.loads((Path(__file__).resolve().parents[1] / "config/platforms.json").read_text(encoding="utf-8"))


def supports(platform, kind):
    entry = REGISTRY.get(platform, {})
    return entry.get("businessAdapter") == "pdd" or kind in entry.get("businessKinds", [])


def source_page(platform, kind):
    if not supports(platform, kind):
        raise ValueError("该平台此模块的数据采集尚未接入")
    if kind == "tax" and platform == "xiaohongshu":
        from .xiaohongshu_tax import PAGE

        return PAGE
    if kind == "tax" and platform in ("taobao", "jd"):
        from .platform_tax import PAGES

        return PAGES[platform]
    if platform == "taobao":
        from .taobao import PAGES
    else:
        from .business_reader import PAGES
    return PAGES[kind]
