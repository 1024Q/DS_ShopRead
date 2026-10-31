"""Deterministic node scheduling policy. No IO, tasks, browsers or databases.

Plans and execution reservations are disjoint. A reservation owns the business
mutex until its worker has actually exited, including cancellation cleanup.
Only reading/reserved phases consume a node read slot.
"""
from dataclasses import dataclass
from collections import Counter
from dataclasses import replace


@dataclass(frozen=True)
class Demand:
    key: tuple
    kind: str
    due_at: float
    waiting_since: float
    interval: float = 60
    retry_at: float = 0
    blocked: str = ''
    enabled: bool = True
    logged_in: bool = True
    browser_ready: bool = True
    database_pressure: bool = False
    dependencies: tuple = ()
    blocked_code: str = 'browser'


@dataclass
class Reservation:
    key: tuple
    kind: str
    token: int
    started_at: float
    phase: str = 'reserved'
    cancel_requested: bool = False


@dataclass(frozen=True)
class Wait:
    code: str
    reason: str
    condition: str
    since: float
    until: float | None = None
    blocker: tuple | None = None

    def fields(self):
        return dict(wait_code=self.code, wait_reason=self.reason, unblock_condition=self.condition,
                    waiting_since=self.since, unblock_at=self.until, blocked_by=self.blocker)


REASONS = {
    'paused': ('计划已暂停或停用', '手动恢复此计划后执行'),
    'login': ('等待店铺登录验证', '完成登录或验证码验证'),
    'browser': ('等待浏览器准备或恢复', '浏览器与登录就绪后派发'),
    'retry': ('失败重试冷却中', '到达重试时间后重新核验'),
    'not_due': ('尚未到达执行时间', '到达下次执行时间'),
    'dependency': ('等待依赖步骤完成', '依赖步骤成功并提交断点'),
    'database': ('本地写入积压，暂停新的读取批次', '已提交批次入库完成、写入压力下降'),
    'module': ('等待同店同业务模块释放', '旧执行及其取消清理完全结束'),
    'shop': ('已达到单店执行并发上限', '本店已有执行步骤结束'),
    'shops': ('等待活跃店铺名额', '其他店铺完成安全批次并释放名额'),
    'node': ('等待节点读取名额', '其他读取结束或进入独立入库阶段'),
    'turn': ('店铺本轮让位中', '当前批次结束，其他到期店铺先执行'),
    'control': ('等待人工或登录独占操作结束', '控制会话退出并释放浏览器读锁'),
    'lease': ('等待旧执行占位核验', '确认旧执行结束后释放持久化占位'),
    'blocked': ('异常需要人工处理', '修复权限、页面或数据异常后重试'),
}


class SchedulingKernel:
    def __init__(self):
        self.reservations = {}
        self.preparing_shops = set()
        self.last_turn = {}
        self.turn_started = {}
        self.yielding = set()
        self.serial = 0
        self.generation = 0

    @staticmethod
    def shop_limit(config, sid):
        return max(1, config.get('shop_limits', {}).get(str(sid), config.get('shop_concurrent', 3)))

    def counts(self):
        phases = Counter(r.phase for r in self.reservations.values())
        return dict(reading=phases['reading'] + phases['reserved'],
                    database=phases['database'], resuming=phases['resuming'],
                    finishing=phases['finishing'], executions=len(self.reservations),
                    preparing_shops=len(self.preparing_shops))

    def release(self, key, token=None):
        current = self.reservations.get(key)
        if not current or token is not None and current.token != token:
            return False
        self.reservations.pop(key)
        sid = key[0]
        if not any(k[0] == sid for k in self.reservations) and sid not in self.preparing_shops:
            self.turn_started.pop(sid, None)
            self.yielding.discard(sid)
        return True

    def phase(self, key, token, phase):
        value = self.reservations.get(key)
        if value and value.token == token:
            value.phase = phase
            return True
        return False

    def cancel(self, key):
        value = self.reservations.get(key)
        if value:
            value.cancel_requested = True

    def _wait(self, demand, code, until=None, blocker=None, detail=''):
        reason, condition = REASONS[code]
        return Wait(code, detail or reason, condition, demand.waiting_since, until, blocker)

    def base_wait(self, demand, now):
        if not demand.enabled:return self._wait(demand, 'paused')
        if not demand.logged_in:return self._wait(demand, 'login')
        if demand.blocked:return self._wait(demand, demand.blocked_code, detail=demand.blocked)
        if demand.retry_at > now:return self._wait(demand, 'retry', demand.retry_at)
        if demand.due_at > now:return self._wait(demand, 'not_due', demand.due_at)
        if demand.dependencies:return self._wait(demand, 'dependency', blocker=demand.dependencies[0])
        if demand.database_pressure:return self._wait(demand, 'database')
        if not demand.browser_ready:return self._wait(demand, 'browser')

    def refresh_turns(self, demands, config, now):
        active = {k[0] for k in self.reservations} | self.preparing_shops
        # Sleeping shops are eligible peers: otherwise a warm busy shop can
        # monopolize the active-shop limit forever and nobody wakes its peers.
        ready = {d.key[0] for d in demands if not self.base_wait(replace(d, browser_ready=True), now)}
        quantum = max(1, config.get('batch_seconds', 60))
        # Browser startup owns no execution turn. Charging its wall time here
        # could mark a shop as yielding before its first reader ever starts.
        for sid in {k[0] for k in self.reservations}:
            self.turn_started.setdefault(sid, now)
            if ready - active and now - self.turn_started[sid] >= quantum:
                self.yielding.add(sid)

    def wait(self, demand, config, now, *, resume=False):
        # An unopened browser may itself be waiting for an active-shop place.
        # Report that capacity constraint before claiming recovery is running.
        reason = self.base_wait(replace(demand, browser_ready=True), now)
        if reason:return reason
        if not resume:
            blocker = next((r.key for r in self.reservations.values()
                            if r.key[0] == demand.key[0] and r.kind == demand.kind), None)
            if blocker:return self._wait(demand, 'module', blocker=blocker)
            if sum(k[0] == demand.key[0] for k in self.reservations) >= self.shop_limit(config, demand.key[0]):
                return self._wait(demand, 'shop')
            if demand.key[0] in self.yielding:return self._wait(demand, 'turn')
        active = {k[0] for k in self.reservations} | self.preparing_shops
        if demand.key[0] not in active and len(active) >= config.get('max_active_shops', 2):
            return self._wait(demand, 'shops')
        if not demand.browser_ready:return self._wait(demand, 'browser')
        counts = self.counts()
        # A derived write-buffer safeguard, never another configurable queue.
        # Existing readers may finish; no unbounded new batches enter a stall.
        if not resume and counts['database'] >= 2 * max(1, config['max_concurrent']):
            return self._wait(demand, 'database')
        if counts['reading'] >= config['max_concurrent']:
            return self._wait(demand, 'node')

    def choose(self, demands, config, now, resumes=()):
        demands = list(demands)
        resume_keys = set(resumes)
        self.refresh_turns(demands, config, now)
        candidates = {d.key:d for d in demands}
        granted = []
        while candidates:
            possible = [d for d in candidates.values() if not self.wait(d, config, now, resume=d.key in resume_keys)]
            if not possible:break
            def order(d):
                # Fairness is per shop first; a shop with many failed demands
                # cannot buy more turns. Within that shop use normalized age.
                return (self.last_turn.get(d.key[0], 0),
                        -max(0, now-d.due_at)/max(1, d.interval), d.waiting_since, str(d.key))
            demand = min(possible, key=order)
            self.serial += 1
            self.last_turn[demand.key[0]] = self.serial
            self.turn_started.setdefault(demand.key[0], now)
            if demand.key in resume_keys:
                allocation = self.reservations.get(demand.key)
                if not allocation:
                    candidates.pop(demand.key);continue
                allocation.phase = 'reading'
            else:
                self.generation += 1
                allocation = Reservation(demand.key, demand.kind, self.generation, now)
                self.reservations[demand.key] = allocation
            granted.append(allocation)
            candidates.pop(demand.key)
        waits = {key:self.wait(d, config, now, resume=key in resume_keys) for key,d in candidates.items()}
        return granted, {k:v for k,v in waits.items() if v is not None}
