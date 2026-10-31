"""The single, platform-specific definition of order/aftersales attention."""

VERSION = 2
PROJECTION_VERSION = 3
KINDS = {"aftersales", "evidence", "tickets", "intercepts", "packages", "invoices"}
LABELS = {
    "evidence": {"待举证"},
    "aftersales": {"平台介入待商家处理", "平台接入待商家处理", "平台处理中"},
    "tickets": {"2小时内将逾期"},
    "intercepts": {"拦截失败"},
    "packages": {"即将发货-揽收超时"},
    "invoices": {"3天内需开票"},
}


def immediate(kind, row, platform=None):
    return ((platform or row.get("platform") or "pdd") == "pdd" and kind == "aftersales"
            and row.get("status") == "平台处理中" and not row.get("attention_excluded"))


def reason(kind, row, platform=None):
    if (platform or row.get("platform") or "pdd") != "pdd" or kind not in KINDS:
        return ""
    if row.get("attention_excluded"):
        return ""
    # attention_status is assigned only by the platform's matching read filter.
    for label in (row.get("attention_status", ""), row.get("status", "")):
        label = str(label).strip().replace("－", "-").replace("—", "-").replace(" ", "")
        if label in LABELS[kind]:
            return label
    return ""
