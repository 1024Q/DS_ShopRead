from .read_errors import LoginPending
import sqlite3


def database_busy(error):
    return isinstance(error, sqlite3.OperationalError) and any(
        word in str(error).lower() for word in ("locked", "busy")
    )


def classify(error):
    if getattr(error, "cooldown_scope", None) == "shop":
        return "shop_cooldown", False
    if database_busy(error) or "数据库写入繁忙" in str(error):
        return "database_busy", False
    if isinstance(error, sqlite3.Error):
        return "database_error", False
    message = str(error)
    if "下单时间超出查询范围" in message:
        return "range_limit", True
    if any(v in message for v in ("操作太过频繁", "请求频繁", "请求过于频繁", "限流", "访问频繁")):
        return "rate_limit", False
    if "订单分段查询仍被平台拒绝" in message:
        return "range_limit", True
    if isinstance(error, LoginPending):
        return "login", True
    if "近 90 日请在阿里妈妈官方报表查询" in message:
        return "unsupported", True
    if any(s in message for s in ("没有当前模块的查看权限", "权限不足", "无查看权限")):
        return "permission", True
    if any(
        s in message
        for s in (
            "字段发生变化",
            "结构发生变化",
            "格式发生变化",
            "归因设置已变化",
            "统计日期不一致",
            "账户与店铺不一致",
            "店铺与配置不一致",
            "页面结构变化",
            "找不到必要字段",
            "筛选状态不一致",
        )
    ):
        return "adapter", True
    import httpx, websockets

    if isinstance(
        error, (TimeoutError, ConnectionError, OSError, httpx.TransportError, websockets.WebSocketException)
    ) or any(s in message.lower() for s in ("timeout", "超时", "连接中断", "连接失败", "断开")):
        return "network", False
    return "transient", False


def guidance(kind, message, category="", blocked=False):
    if category == "shop_cooldown":
        return {"automatic": True, "reason": "同店其他接口短暂冷却", "action": "保留当前数据和断点，短暂冷却结束后恢复，不扩大为该模块五分钟限流。"}
    if category == "database_error":
        return {
            "automatic": True,
            "reason": message,
            "action": "保留已提交数据与断点；请检查采集端数据库结构和文件权限，修复后按计划重试。此错误无需重新登录店铺。",
        }
    if category == "database_busy":
        return {
            "automatic": True,
            "reason": "采集端数据库写入排队超时",
            "action": "系统按顺序等待写入并有限重试；保留已提交的分页断点。持续发生时检查采集节点磁盘负载和数据库长事务，无需重新登录店铺。",
        }
    if category == "rate_limit":
        return {
            "automatic": True,
            "reason": "平台限制订单查询频率" if kind == "orders" else "平台限制该模块接口请求频率",
            "action": "同店铺接口请求按顺序执行，该模块至少冷却 5 分钟，保留分页断点后继续；冷却期间同店同模块的其他任务暂不领取，请勿连续手动重试。",
        }
    if category == "permission":
        return {
            "automatic": False,
            "reason": "平台拒绝当前账号访问该模块",
            "action": "点击查看后台，用主账号检查子账号的模块查看权限；品牌黑标还需确认店铺是否具备该权益。授权或开通后点击重试；无此权益可在模块采集方式中暂停该模块。",
        }
    if category == "range_limit" or "订单分段查询仍被平台拒绝" in message:
        return {
            "automatic": False,
            "reason": message,
            "action": "在官方后台按错误对应日期查询。若已超出平台历史保留期，缩小日期范围重新采集；若仍在可查范围，检查账号查询权限或联系平台客服。",
        }
    if category == "login":
        return {
            "automatic": False,
            "reason": "登录失效或需要人工验证",
            "action": "点击查看后台，完成登录、扫码或验证码；登录恢复后系统继续重试。",
        }
    if category == "adapter":
        return {
            "automatic": False,
            "reason": message,
            "action": "点击查看后台核对页面是否改版、字段缺失或店铺身份不一致；保留错误信息修正对应采集适配后再重试。",
        }
    if category == "unsupported":
        return {
            "automatic": False,
            "reason": message,
            "action": "选择该平台支持的日期范围重新采集；不支持的历史范围请到平台官方报表导出。",
        }
    if kind == "orders" and any(s in message for s in ("查询范围", "可查询量", "订单查询异常")):
        return {
            "automatic": True,
            "reason": "订单查询范围或数量超过平台单次限制",
            "action": "系统按原日期范围分段查询，遇到上限继续细分，保存断点并合并去重；全部完成后更新正式数据。",
        }
    return {
        "automatic": not blocked,
        "reason": message,
        "action": "系统按重试计划恢复读取；单页异常优先重建采集页，确认登录或浏览器失效后才恢复店铺，分页进度保留。持续失败时点击查看后台检查网络和页面提示。",
    }


def describe(error):
    if database_busy(error):
        return "采集端数据库写入繁忙，排队等待超时；保留已提交的分页断点，按重试计划恢复"
    if isinstance(error, sqlite3.Error):
        message = str(error).lower()
        reason = next(
            (
                label
                for token, label in [
                    ("readonly", "数据库写入目标只读"),
                    ("no such table", "数据库缺少业务表"),
                    ("no such column", "数据库缺少业务字段"),
                    ("disk is full", "磁盘空间不足"),
                    ("malformed", "数据库完整性异常"),
                ]
                if token in message
            ),
            "数据库执行异常",
        )
        return f"{reason}（{getattr(error, 'sqlite_errorname', type(error).__name__)}）；保留已提交数据和断点，修复数据库后重试"
    if isinstance(error, ValueError):
        return str(error)
    import httpx
    import websockets

    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "采集读取超时；保留已提交的分页进度，按重试计划继续"
    if isinstance(error, (ConnectionError, OSError, httpx.TransportError, websockets.WebSocketException)):
        return "浏览器或网络连接中断；系统自动重连，浏览器退出时自动启动，登录恢复后继续采集"
    import traceback, re

    frames = traceback.extract_tb(error.__traceback__)
    location = " → ".join(f"{f.filename.replace(chr(92), chr(47)).split(chr(47))[-1]}:{f.lineno}" for f in frames[-4:])
    field = str(error.args[0]) if isinstance(error, KeyError) and error.args else ""
    field = field if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,60}", field) else ""
    detail = " · ".join(v for v in (field, location) if v)
    return (
        f"采集执行异常（{type(error).__name__}）"
        + (" · " + detail if detail else "")
        + "；按重试计划恢复，持续失败请查看运行日志"
    )
