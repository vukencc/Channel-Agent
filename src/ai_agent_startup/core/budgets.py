"""本地估算费用与请求速率账本；状态目录的单写者锁由 SessionStore 持有。"""
import asyncio
import copy
import json
import math
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from ai_agent_startup import config
from ai_agent_startup.core.storage import SessionStore


class BudgetExceeded(RuntimeError):
    """预算不足时保存检查点，不能通过重试绕过。"""


class BudgetLedger:
    def __init__(self, root):
        self.path = root / 'budget.json'
        self.lock = threading.RLock()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'sessions': {}, 'days': {}, 'requests': [], 'pending': {}}

    def _commit(self, state):
        SessionStore.atomic_write(self.path, json.dumps(state, ensure_ascii=False))
        self.state = state

    def reserve(self, session_id, cost):
        with self.lock:
            if config.SESSION_COST_LIMIT or config.DAILY_COST_LIMIT:
                if cost is None or not math.isfinite(cost) or cost < 0:
                    raise BudgetExceeded('费用预算已启用，但模型单价未知；请配置正的输入/输出单价。')
            cost = cost or 0
            stamp = time.time()
            day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
            state = copy.deepcopy(self.state)
            requests = [value for value in state['requests'] if stamp - value < 60]
            if config.MODEL_REQUESTS_PER_MINUTE and len(requests) >= config.MODEL_REQUESTS_PER_MINUTE:
                raise BudgetExceeded('已达到 MODEL_REQUESTS_PER_MINUTE；请稍后继续。')
            session_total = state['sessions'].get(session_id, 0) + cost
            day_total = state['days'].get(day, 0) + cost
            if config.SESSION_COST_LIMIT and session_total > config.SESSION_COST_LIMIT:
                raise BudgetExceeded('预计费用超过 SESSION_COST_LIMIT；已暂停，尚未发送请求。')
            if config.DAILY_COST_LIMIT and day_total > config.DAILY_COST_LIMIT:
                raise BudgetExceeded('预计费用超过 DAILY_COST_LIMIT（UTC 日）；已暂停，尚未发送请求。')
            ticket = uuid.uuid4().hex
            state['sessions'][session_id] = session_total
            state['days'][day] = day_total
            state['requests'] = requests + [stamp] if config.MODEL_REQUESTS_PER_MINUTE else []
            state['pending'][ticket] = {'session': session_id, 'day': day, 'reserved': cost}
            self._commit(state)
            return ticket

    def settle(self, ticket, actual):
        with self.lock:
            if ticket not in self.state['pending']:
                return
            state = copy.deepcopy(self.state)
            item = state['pending'].pop(ticket)
            if actual is not None:
                if not math.isfinite(actual) or actual < 0:
                    raise ValueError('费用必须为非负有限数字')
                difference = actual - item['reserved']
                state['sessions'][item['session']] += difference
                state['days'][item['day']] += difference
            # 无 usage/中断时保留预留费用，不能把未知费用记为零。
            self._commit(state)


active_budget = ContextVar('active_budget', default=None)


@contextmanager
def budget_scope(ledger, session_id):
    token = active_budget.set((ledger, session_id) if ledger else None)
    try:
        yield
    finally:
        active_budget.reset(token)


async def reserve_request(cost):
    value = active_budget.get()
    if value is None:
        return None
    ledger, session_id = value
    return ledger, await asyncio.to_thread(ledger.reserve, session_id, cost)


async def settle_request(ticket, cost):
    if ticket is not None:
        ledger, identifier = ticket
        await asyncio.to_thread(ledger.settle, identifier, cost)
