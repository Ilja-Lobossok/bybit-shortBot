"""Local entry gate for confirmed Bybit agreement-required rejections only."""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time

logger = logging.getLogger(__name__)
TTL = 24 * 3600
MAX_SYMBOLS = 256


def valid_symbol(value):
    return isinstance(value, str) and 1 <= len(value) <= 40 and value.endswith('USDT') and all(c.isalnum() or c in '_.-' for c in value)


class ContractAvailability:
    def __init__(self, path, *, log_path=None, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.blocked = {}
        now = clock()
        try:
            if self.path.exists():
                with self.path.open('rb') as source:
                    raw = source.read(64 * 1024 + 1)
                value = json.loads(raw) if len(raw) <= 64 * 1024 else {}
                if value.get('version') == 1:
                    self.blocked = {k: float(v) for k, v in value.get('blocked', {}).items()
                                    if valid_symbol(k) and now < float(v) <= now + TTL}
            # Bootstrap only this bot's recent, explicit 110126 error lines.
            # No HTTP probing, order attempts, agreements or arbitrary text saved.
            if log_path and Path(log_path).exists():
                with Path(log_path).open('rb') as source:
                    source.seek(max(0, source.seek(0, 2) - 2 * 1024**2))
                    raw = source.read(2 * 1024**2).decode('utf-8', errors='replace')
                pattern = r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*Ошибка открытия позиции ([\w.-]+): .*\(ErrCode:\s*110126\)'
                for line in raw.splitlines():
                    match = re.search(pattern, line)
                    if match and valid_symbol(match[2]):
                        stamp = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
                        if 0 <= now - stamp < TTL:
                            self.blocked[match[2]] = max(self.blocked.get(match[2], 0), stamp + TTL)
            self._bound(now)
        except Exception:
            logger.warning('Contract availability history could not be fully loaded; details suppressed.')

    def _bound(self, now):
        self.blocked = dict(sorted(((k, v) for k, v in self.blocked.items() if v > now),
                                   key=lambda item: item[1], reverse=True)[:MAX_SYMBOLS])

    def allows_entry(self, symbol):
        return self.blocked.get(symbol, 0) <= self.clock()

    def record(self, symbol, error):
        # Classify the original SDK exception without changing or retrying it.
        if not valid_symbol(symbol) or getattr(error, 'status_code', None) != 110126:
            return
        now = self.clock()
        self.blocked[symbol] = now + TTL
        self._bound(now)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump({'version': 1, 'blocked': self.blocked}, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, self.path)
        except Exception:
            logger.warning('Contract availability stays in memory; persistence failed. Details suppressed.')
