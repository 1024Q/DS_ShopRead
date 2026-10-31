"""Finite shop turns, yielding only between already-running collection steps."""

import time


class ShopRotation:
    def __init__(self):
        self.rounds = {}
        self.last = {}
        self.started = {}
        self.budgets = {}
        self.yielding = set()

    def finish(self, sid, now=None):
        self.rounds.pop(sid, None)
        self.started.pop(sid, None)
        self.budgets.pop(sid, None)
        self.yielding.discard(sid)
        self.last[sid] = time.monotonic() if now is None else now

    def refresh(self, progress, candidates=(), config=None, grants=(), now=None):
        now = time.monotonic() if now is None else now
        active = {k for k, m in progress.items() if m.get("phase") == "running"} | set(grants)
        known = {k for k, m in progress.items() if m.get("phase") in ("queued", "running")} | set(candidates)
        # Only a runnable peer can cause a time-budget yield. A blocked login or
        # pressured database must not force another healthy shop to idle.
        ready = {
            k[0]
            for k, m in progress.items()
            if m.get("phase") == "queued"
            and not any(m.get(f) for f in ("blocked", "database_pressure", "browser_blocked", "clearing_queue"))
            and not (m.get("shared_result_ready") and not m.get("force"))
            and m.get("retry_at", 0) <= time.time()
        }
        from .refund_priority import expedited

        urgent = {key[0] for key, meta in progress.items() if key[0] in ready
                  and meta.get("phase") == "queued" and expedited(meta)
                  and not any(meta.get(f) for f in ("blocked", "database_pressure", "browser_blocked", "clearing_queue"))
                  and meta.get("retry_at", 0) <= time.time()}
        for sid, keys in list(self.rounds.items()):
            keys.intersection_update(known)
            peers = ready - set(self.rounds)
            budget = self.budgets.get(sid)
            if budget is not None and peers and now - self.started[sid] >= budget:
                self.yielding.add(sid)
            if urgent - {sid} and sid not in urgent:
                # Yield only between running readers, never cancel a live task.
                self.yielding.add(sid)
            # An expired turn does not interrupt a reader or revoke a browser
            # wake already granted. Queued/latent steps retain their own ledger.
            if not keys or sid in self.yielding and not any(k[0] == sid for k in active):
                self.finish(sid, now)
        for key, meta in progress.items():
            sid = key[0]
            meta["shop_round_yielding"] = sid in self.yielding
            meta["shop_round_wait"] = sid in self.rounds and (key not in self.rounds[sid] or sid in self.yielding)
            meta["shop_turn_order"] = -1 if sid in self.rounds else self.last.get(sid, 0)
            meta["shop_round_budget_seconds"] = self.budgets.get(sid)
            meta["shop_round_elapsed_seconds"] = max(0, round(now - self.started[sid])) if sid in self.started else None

    def reserve(self, key, progress, candidates=(), config=None, now=None):
        sid = key[0]
        if sid not in self.rounds:
            # Freeze all demands already due at the start, including an
            # alternate scope of the same business still outside worker jobs.
            # New due periods cannot extend this set while the turn is active.
            self.rounds[sid] = {
                k for k, m in progress.items() if k[0] == sid and m.get("phase") in ("queued", "running")
            }
            self.rounds[sid].update(k for k in candidates if k[0] == sid)
            self.started[sid] = time.monotonic() if now is None else now
            self.budgets[sid] = (
                max(1, config.get("batch_seconds", 60)) if config and config.get("frequency_version") else None
            )
        self.rounds[sid].add(key)

    def allows(self, key):
        return key[0] not in self.rounds or key[0] not in self.yielding and key in self.rounds[key[0]]

    def complete(self, key):
        keys = self.rounds.get(key[0])
        if keys is not None:
            keys.discard(key)
            if not keys:
                self.finish(key[0])


def is_home(kind, scope):
    from .frequency_policy import task_id
    from .task_catalog import HOME

    return task_id(kind, scope) in HOME


def home_paused(kind, scope, config):
    return bool(config.get("home_paused") and is_home(kind, scope))
