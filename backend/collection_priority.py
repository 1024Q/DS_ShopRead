"""Compatibility accessors for old clients; priority no longer affects scheduling."""

LEVELS = ("high", "medium", "low")
CORE_MODULES = frozenset()


def level(kind, scope, config, platform="pdd"):
    return "medium"


def rank(kind, scope, config, platform="pdd"):
    return 1


def meta_rank(meta, config):
    return 1
