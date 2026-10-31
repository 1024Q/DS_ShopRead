"""Three cadence queues; retain the legacy policy for code rollback."""


def migrate(config):
    from .business import KINDS
    from .platforms import PLATFORMS
    from .collection import mode, interval_seconds
    from .platform_support import supports

    result = dict(config)
    modes = {}
    queues = {}
    for platform in PLATFORMS:
        modes[platform] = {}
        queues[platform] = {}
        for kind in KINDS:
            if not supports(platform, kind):
                continue
            cadence = mode(kind, {}, config, platform)
            target = (
                "off"
                if cadence == "off"
                else "daily"
                if cadence == "daily"
                else "realtime"
                if interval_seconds(kind, config, platform) == 60
                else "custom"
            )
            modes[platform][kind] = target
            queues[platform][kind] = {"realtime": "focus", "custom": "normal", "daily": "daily", "off": "off"}[target]
    total = max(3, config["max_concurrent"])
    daily = max(1, total // 8)
    realtime = max(1, total // 2)
    normal = total - daily - realtime
    result.update(
        queue_version=3,
        platform_modes=modes,
        platform_queues=queues,
        module_modes={},
        platform_intervals={},
        max_concurrent=total,
        realtime_concurrent=realtime,
        daily_concurrent=normal,
        scheduled_concurrent=daily,
        borrow_idle=False,
        realtime_seconds=60,
        custom_seconds=300,
        change_adaptive_enabled=False,
    )
    result["frequency_names"] = {
        **config["frequency_names"],
        "realtime": "实时采集",
        "custom": "常规采集",
        "daily": "每日采集",
    }
    return result


def synchronize(config):
    """Frequency and queue are one assignment; priority remains independent."""
    config["platform_queues"] = {
        p: {k: {"realtime": "focus", "custom": "normal", "daily": "daily", "off": "off"}[v] for k, v in modes.items()}
        for p, modes in config["platform_modes"].items()
    }
    return config
