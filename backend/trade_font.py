"""Recognize rendered digits against templates verified with numeric source data.
The font randomizes code points and slightly perturbs outlines. Strict confidence
thresholds prevent silently guessing money values.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent
TEMPLATES = {digit: int(mask, 16) for digit, mask in json.loads((ROOT / "trade_digits.json").read_text()).items()}
# A formatter may terminate the standalone arrow-function statement. CDP
# embeds it as an expression, where that trailing semicolon is invalid.
RENDER = (ROOT / "trade_glyphs.js").read_text(encoding="utf-8").strip().removesuffix(";")


def recognize(masks):
    result = {}
    for code, mask in masks.items():
        value = int(mask, 16)
        scores = sorted(
            (((value & t).bit_count() / (value | t).bit_count(), d) for d, t in TEMPLATES.items()), reverse=True
        )
        if scores[0][0] < 0.88 or scores[0][0] - scores[1][0] < 0.08:
            raise ValueError("交易数字字形无法可靠识别，未保存本次金额，请稍后重试或更新读取适配")
        result[code] = scores[0][1]
    if len(set(result.values())) != len(result):
        raise ValueError("交易数字字形校验失败，请更新读取适配")
    return str.maketrans(result)


async def display_map(page, values):
    chars = sorted({c for c in json.dumps(values, ensure_ascii=False) if "\ue000" <= c <= "\uf8ff"})
    if not chars:
        return {}
    masks = await page.evaluate("(" + RENDER + ")(" + json.dumps(chars) + ")")
    return recognize(masks)
