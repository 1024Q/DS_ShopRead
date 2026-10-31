"""A warning query cannot turn completed/buyer/platform cases into merchant work."""

ENDED = ("退款成功", "退款关闭", "换货关闭", "确认完成换货", "补寄关闭", "确认完成补寄", "已关闭", "已完成")


def pending_merchant(title):
    title = str(title or "")
    return "待商家" in title and not any(word in title for word in ENDED)


def actual_status(row):
    if row.get("status") != "待处理即将逾期":
        return row
    title = row.get("progress") or "未知"
    return {**row, "status": title, "attention_status": "待处理即将逾期" if pending_merchant(title) else ""}
