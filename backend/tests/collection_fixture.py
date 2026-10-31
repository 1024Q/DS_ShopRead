"""Current task-group fixtures; never modify the developer's node settings."""

import asyncio
from backend import collection
from backend.frequency_policy import catalog, migrate
from backend.task_catalog import grouped_id


def policy(**updates):
    """A current policy without production's one-time home-only pause."""
    result = migrate(collection.DEFAULTS)
    result.update(updates)
    return result


def assign(store, assignments, *, platform="pdd", reset=False):
    """Use the public patch contract, including merged task identifiers."""
    modes = {item["id"]: "off" for item in catalog()} if reset else {}
    modes.update({grouped_id(key): value for key, value in assignments.items()})
    return collection.save_settings(
        store,
        {
            "module_assignments": [
                {"platform": platform, "kind": key, "tab": "task", "target": target} for key, target in modes.items()
            ]
        },
    )


async def drain(service, timeout=10):
    async def complete():
        while service.jobs:
            await asyncio.gather(*list(service.jobs.values()))

    await asyncio.wait_for(complete(), timeout)
