"""Machine-local admission tuning, bounded by the user's configured maximum."""

import ctypes, math, os, time
from pathlib import Path


class LoadController:
    def __init__(self):
        self.limit = None
        self.last_change = 0
        self.previous = None
        self.status = {}
        self.started = time.time()
        self.warmup = int(
            os.environ.get(
                "ECMP_UPDATE_WARMUP_SECONDS",
                "300"
                if (
                    Path(os.environ.get("PDD_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".data")))
                    / "updates/journal.json"
                ).exists()
                else "0",
            )
        )

    def sample(self):
        if os.name != "nt":
            return None, None
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (k, ctypes.c_ulonglong)
                for k in ("total", "avail", "page", "avail_page", "virtual", "avail_virtual", "extended")
            ]

        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        used = float(memory.load) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) else None
        values = [wintypes.FILETIME() for _ in range(3)]
        if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(v) for v in values)):
            return None, used
        idle, kernel, user = [(v.dwHighDateTime << 32) + v.dwLowDateTime for v in values]
        previous = self.previous
        self.previous = (idle, kernel, user)
        cpu = None
        if previous:
            total = kernel + user - previous[1] - previous[2]
            if total > 0:
                cpu = max(0, min(100, 100 * (1 - (idle - previous[0]) / total)))
        return cpu, used

    def update(self, config, events=(), running=0, now=None, measurements=None):
        now = time.time() if now is None else now
        try:
            cpu, memory = measurements if measurements is not None else self.sample()
        except OSError, AttributeError:
            cpu, memory = None, None
        maximum = config["max_concurrent"]
        self.limit = min(self.limit or maximum, maximum)
        recent = [e for e in events if e[0] > now - 300]
        fail = sum(not e[1] for e in recent) / len(recent) if recent else 0
        failing = len(recent) >= 8 and fail >= 0.3
        reason = "正常负载"
        target = maximum
        # Standing browser memory is not all caused by active reads. Moderate
        # pressure must not ratchet to two slots without any memory improvement.
        if (cpu is not None and cpu >= 95) or (memory is not None and memory >= 97):
            target = 2
        elif (cpu is not None and cpu >= 85) or (memory is not None and memory >= 93):
            target = max(2, math.ceil(maximum / 2))
        elif memory is not None and memory >= 85:
            target = max(2, math.ceil(maximum * 0.75))
        if failing:
            target = min(target, max(2, math.ceil(maximum / 2)))
        if not config.get("adaptive_enabled", True):
            self.limit = maximum
            reason = "手动固定并发"
        elif self.limit > target and now - self.last_change >= 30:
            self.limit = max(target, self.limit - 1)
            self.last_change = now
            reason = "失败率升高，降低访问并发" if failing else "机器负载偏高，分级降载"
        elif (
            self.limit < target
            and cpu is not None
            and memory is not None
            and cpu < 65
            and memory < 93
            and now - self.last_change >= 120
        ):
            self.limit = min(target, self.limit + 1)
            self.last_change = now
            reason = "负载稳定，逐步恢复并发"
        elif self.limit == target and target < maximum:
            reason = "保持适合当前负载的并发"
        elif self.limit < maximum:
            reason = "等待负载稳定后逐步恢复"
        effective = self.limit
        if self.warmup and now - self.started < self.warmup:
            effective = min(effective, 2 + 2 * max(0, int((now - self.started) // 30)))
            reason = "升级试运行中，逐步恢复采集并发"
        if config.get("queue_version") == 3:
            effective = max(3, effective)
            fields = ("realtime_concurrent", "daily_concurrent", "scheduled_concurrent")
            slots = {k: 1 for k in fields}
            for _ in range(effective - 3):
                key = max(fields, key=lambda k: config[k] * effective / maximum - slots[k])
                slots[key] += 1
            self.status = {
                "cpu_percent": round(cpu, 1) if cpu is not None else None,
                "memory_percent": memory,
                "configured_limit": maximum,
                "effective_limit": effective,
                "target_limit": max(3, target),
                "reason": reason,
                "running": running,
            }
            return {"max_concurrent": effective, **slots}
        daily = max(1, min(effective - 1, round(effective * config["daily_concurrent"] / maximum)))
        self.status = {
            "cpu_percent": round(cpu, 1) if cpu is not None else None,
            "memory_percent": memory,
            "configured_limit": maximum,
            "effective_limit": effective,
            "target_limit": target,
            "reason": reason,
            "running": running,
        }
        return {"max_concurrent": effective, "realtime_concurrent": effective - daily, "daily_concurrent": daily}
