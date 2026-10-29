import json
import time
import logging
import os
from typing import Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class PositionTracker:
    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.positions: Dict[str, Any] = {}
        self.cooldowns: Dict[str, int] = {}
        self.watchlist: Dict[str, Any] = {}
        self.risk_state: Dict[str, Any] = {}
        self.load()
    
    def load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if any(not isinstance(data.get(key, {}), dict) for key in
                           ('positions', 'cooldowns', 'watchlist', 'risk_state')):
                        raise ValueError("Malformed state file")
                    self.positions = data.get('positions', {})
                    self.cooldowns = data.get('cooldowns', {})
                    self.watchlist = data.get('watchlist', {})
                    self.risk_state = data.get('risk_state', {})
                    logger.info(f"Loaded {len(self.positions)} positions, {len(self.watchlist)} watchlist")
            except Exception as e:
                logger.error(f"Error loading state: {e}")
                raise  # Never overwrite unreadable live positions with an empty tracker.
    
    def save(self):
        """Сохранить состояние в файл"""
        data = {
            'positions': self.positions,
            'cooldowns': self.cooldowns,
            'watchlist': self.watchlist,
            'risk_state': self.risk_state,
            'updated_at': int(time.time())
        }
        
        # Сохраняем с временным файлом для атомарности
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix('.tmp')
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, self.state_file)

    def close_position(self, symbol, risk_state):
        """Persist the close and its accounting together, or keep both unchanged."""
        previous_position = self.positions.pop(symbol)
        previous_cooldowns = self.cooldowns.copy()
        previous_risk = self.risk_state
        self.cooldowns[symbol] = int(time.time())
        self.risk_state = risk_state
        try:
            self.save()
        except Exception:
            self.positions[symbol] = previous_position
            self.cooldowns = previous_cooldowns
            self.risk_state = previous_risk
            raise
    
    def add_position(self, position: Dict[str, Any]):
        self.positions[position['symbol']] = position
        self.save()
    
    def remove_position(self, symbol: str):
        if symbol in self.positions:
            del self.positions[symbol]
            self.cooldowns[symbol] = int(time.time())
            self.save()
    
    def in_cooldown(self, symbol: str, cooldown_minutes: int) -> bool:
        if symbol not in self.cooldowns:
            return False
        elapsed = int(time.time()) - self.cooldowns[symbol]
        return elapsed < cooldown_minutes * 60
    
    def update_watchlist(self, symbol: str, data: Dict[str, Any]):
        self.watchlist[symbol] = data
        self.save()
    
    def remove_from_watchlist(self, symbol: str):
        if symbol in self.watchlist:
            del self.watchlist[symbol]
            self.save()
    
    def reload_from_file(self):
        """Принудительно перезагрузить состояние из файла"""
        self.load()
        logger.info(f"Перезагружено из файла: {len(self.positions)} positions, {len(self.watchlist)} watchlist")
