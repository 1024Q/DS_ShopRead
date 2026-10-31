"""Goods promotion enums verified against the official goods app, 2026-10-11."""

STATUSES = {
    1: "推广中",
    2: "手动暂停",
    3: "商品售罄",
    4: "商品下架",
    5: "审核中",
    6: "审核驳回",
    7: "到达日限额",
    8: "余额不足",
    9: "推广受限",
    11: "规则暂停",
    12: "未缴纳保证金",
    14: "系统暂停-投放未开始",
    15: "系统暂停-投放已结束",
    21: "系统暂停",
    25: "无可用创意",
    26: "无可用创意",
}


def label(code):
    return STATUSES.get(int(code), "未知推广状态") if str(code).isdigit() else "未知推广状态"


def normalize(row):
    return {**row, "status": label(row.get("status_code"))} if row.get("status_code") is not None else row
