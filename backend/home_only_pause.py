"""One-time fleet pause carried by the code update; explicit UI assignments resume it."""

import copy

REVISION = "20261015-home-only-1"
MARKER = "home_only_pause_revision"
BACKUP = "collection_policy_before_home_only_20261015"


def apply(config, entries):
    if config.get(MARKER) == REVISION:
        return config
    from .platforms import PLATFORMS
    from .task_catalog import HOME, grouped_id, assign

    keep = {grouped_id(key) for key in HOME}
    result = copy.deepcopy(config)
    for platform in PLATFORMS:
        for task in entries:
            if task["id"] not in keep:
                assign(result, platform, task["id"], "off")
    result[MARKER] = REVISION
    return result


def held(kind, scope, config, platform):
    from .shop_rotation import home_paused

    if home_paused(kind, scope, config):
        return True
    if config.get(MARKER) != REVISION:
        return False
    from .frequency_policy import task_id, group
    from .task_catalog import HOME

    return task_id(kind, scope) not in HOME and group(kind, scope, config, platform) is None
