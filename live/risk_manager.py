from copy import deepcopy
from datetime import datetime, timezone
import math

# risk_manager.py

class RiskManager:
    """Управление рисками на основе истории монеты"""
    
    def __init__(self, initial_capital=10000):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        self.today_pnl = 0
        self.current_date = None
        self.coin_stats = {}  # История по каждой монете
        self.consecutive_losses = 0
        self.trades_history = []  # список закрытых сделок

    def sync_capital(self, wallet_balance):
        """Sizing uses an exchange snapshot, never accumulated estimates."""
        if not math.isfinite(wallet_balance):
            raise ValueError("Invalid wallet balance")
        self.current_capital = wallet_balance
        self.peak_capital = max(self.peak_capital, wallet_balance)

    def to_state(self):
        return deepcopy({
            "version": 1, "initial_capital": self.initial_capital,
            "current_capital": self.current_capital, "peak_capital": self.peak_capital,
            "today_pnl": self.today_pnl,
            "current_date": self.current_date.isoformat() if self.current_date else None,
            "coin_stats": self.coin_stats, "consecutive_losses": self.consecutive_losses,
            "trades_history": self.trades_history,
        })

    @classmethod
    def from_state(cls, state, wallet_balance):
        manager = cls(initial_capital=wallet_balance)
        if state:
            if state["version"] != 1:
                raise ValueError("Unsupported risk state version")
            for key in ("initial_capital", "peak_capital", "today_pnl"):
                value = float(state[key])
                if not math.isfinite(value):
                    raise ValueError("Invalid saved risk amount")
                setattr(manager, key, value)
            manager.current_date = datetime.fromisoformat(state["current_date"]).date() if state["current_date"] else None
            manager.coin_stats = deepcopy(state["coin_stats"])
            manager.consecutive_losses = int(state["consecutive_losses"])
            manager.trades_history = deepcopy(state["trades_history"])
        manager.sync_capital(wallet_balance)
        return manager
        
    def update_stats(self, symbol, pnl_percent):
        """Обновляем статистику по монете"""
        if symbol not in self.coin_stats:
            self.coin_stats[symbol] = {
                'trades': 0,
                'profitable': 0,
                'total_pnl': 0,
                'max_loss': 0
            }
        
        stats = self.coin_stats[symbol]
        stats['trades'] += 1
        stats['total_pnl'] += pnl_percent
        
        if pnl_percent > 0:
            stats['profitable'] += 1
        else:
            stats['max_loss'] = min(stats['max_loss'], pnl_percent)
    
    def get_position_multiplier(self, symbol):
        """
        Возвращает множитель размера позиции:
        1.0 = полный базовый размер из BotConfig
        0.5 = половинный базовый размер
        0.25 = четверть базового размера
        """
        if symbol not in self.coin_stats:
            return 0.5  # Новая монета - половинный риск
        
        stats = self.coin_stats[symbol]
        
        # Если меньше 3 сделок - консервативно
        if stats['trades'] < 3:
            return 0.5
        
        # Если были сильные убытки (хуже -200%)
        if stats['max_loss'] < -200:
            return 0.25
        
        # Если винрейт меньше 70%
        win_rate = stats['profitable'] / stats['trades']
        if win_rate < 0.7:
            return 0.5
        
        return 1.0  # Надежная монета
    
    def can_trade_today(self, date, pnl_today=None):
        """Проверка дневных лимитов"""
        if self.current_date != date:
            self.current_date = date
            self.today_pnl = 0
        
        if pnl_today:
            self.today_pnl += pnl_today
        
        # Не торгуем если сегодня убыток больше $500
        if self.today_pnl < -500:
            return False, f"Дневной лимит убытка: {self.today_pnl:.2f}"
        
        # Не торгуем если общая просадка > 20%
        if self.current_capital <= 0 or self.peak_capital <= 0:
            return False, "Нет подтвержденного положительного баланса"
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        if drawdown > 0.20:
            return False, f"Просадка {drawdown:.1%} > 20%"
        
        # Не торгуем если 3 убытка подряд
        if self.consecutive_losses >= 3:
            return False, f"3 убытка подряд"
        
        return True, "OK"
    
    def on_trade_result(self, pnl_usdt, pnl_percent, symbol, closed_at=None, order_ids=None):
        closed_at = closed_at or datetime.now(timezone.utc)
        # Roll the day before adding PnL; a delayed yesterday's close is not today's loss.
        today = datetime.now(timezone.utc).date()
        if self.current_date != today:
            self.current_date = today
            self.today_pnl = 0
        if closed_at.date() == today:
            self.today_pnl += pnl_usdt
        
        self.update_stats(symbol, pnl_percent)
        
        # Добавляем сделку в историю
        self.trades_history.append({
            'time': closed_at.isoformat(),
            'symbol': symbol,
            'pnl_usdt': pnl_usdt,
            'pnl_percent': pnl_percent,
            'order_ids': order_ids or [],
        })
        # Positions can be reconciled in a different order than they closed.
        self.trades_history.sort(key=lambda trade: datetime.fromisoformat(trade['time']))
        self.consecutive_losses = 0
        for trade in reversed(self.trades_history):
            if trade['pnl_usdt'] >= 0:
                break
            self.consecutive_losses += 1
        # Храним только последние 1000 сделок
        if len(self.trades_history) > 1000:
            self.trades_history = self.trades_history[-1000:]
