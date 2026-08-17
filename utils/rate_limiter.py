import time

_PRUNE_THRESHOLD = 2048
_PRUNE_MAX_AGE = 3600.0


class RateLimiter:
    def __init__(self):
        self._user_cooldowns: dict[str, float] = {}
        self._group_cooldowns: dict[str, float] = {}

    def _prune(self, now: float) -> None:
        if len(self._user_cooldowns) <= _PRUNE_THRESHOLD:
            return
        cutoff = now - _PRUNE_MAX_AGE
        for store in (self._user_cooldowns, self._group_cooldowns):
            expired = [k for k, v in store.items() if v < cutoff]
            for k in expired:
                del store[k]

    def _user_key(self, action: str, qq: str, group_id: str) -> str:
        return f"{action}:{qq}:{group_id}"

    def check_user(self, action: str, qq: str, group_id: str, cooldown: int) -> bool:
        if cooldown <= 0:
            return True
        key = self._user_key(action, qq, group_id)
        now = time.time()
        self._prune(now)
        last = self._user_cooldowns.get(key, 0)
        if now - last < cooldown:
            return False
        self._user_cooldowns[key] = now
        return True