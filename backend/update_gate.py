"""Short renewable maintenance lease; never changes saved collection settings."""

import time

deadline = 0.0


def active():
    return time.monotonic() < deadline


def renew(seconds=30):
    # The updater renews every couple of seconds while draining; a short lease bounds the pause if it dies.
    global deadline
    deadline = time.monotonic() + seconds


def release():
    global deadline
    deadline = 0.0


def blockers(business, items):
    dispatcher = getattr(business, 'due_plans', None)
    # Actual reservations survive through final publication. A historical
    # running_shops hint may remain after a worker returns its browser slot;
    # it must not indefinitely block an otherwise idle update.
    collecting = (len({key[0] for key in dispatcher.core.reservations})
                  if dispatcher is not None else len(business.running_shops))
    return {
        "collecting": collecting,
        "busy_windows": sum(bool(item.lock.locked() or getattr(item, 'direct_refund_active', False)) for item in items),
        "remote_controls": sum(
            bool(getattr(item, "remote_control_active", False) or getattr(item, "control_until", 0) > time.time())
            for item in items
        ),
    }
