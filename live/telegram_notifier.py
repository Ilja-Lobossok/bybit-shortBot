import logging
import requests
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        
        if self.enabled:
            self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            return False
        
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            response = requests.post(url, data=data, timeout=10)
            if response.status_code != 200:
                logger.error(f"Telegram error: {response.text}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False
    
    def send_trade_open(self, symbol: str, entry: float, tp: float, sl: float, 
                        size_usdt: float, multiplier: float):
        if not self.enabled:
            return
        
        # Рассчитываем проценты
        tp_pct = ((tp / entry) - 1) * 100
        sl_pct = ((sl / entry) - 1) * 100
        
        local_time = datetime.now(timezone.utc)
        
        text = f"""
🔵 НОВАЯ СДЕЛКА
Символ: {symbol}
Цена входа: ${entry:.4f}
TP: ${tp:.4f} ({tp_pct:+.1f}%)
SL: ${sl:.4f} ({sl_pct:+.1f}%)
Размер: ${size_usdt:.2f} ({multiplier:.1f}x)
Время (UTC): {local_time.strftime('%Y-%m-%d %H:%M:%S')}
"""
        self.send_message(text)
    
    def send_trade_close(self, symbol: str, entry: float, exit_price: float, 
                         pnl_usdt: float, pnl_percent: float, reason: str, duration: str):
        if not self.enabled:
            return
        
        emoji = "🟢" if pnl_usdt > 0 else "🔴"
        sign = "+" if pnl_usdt > 0 else ""
        
        local_time = datetime.now(timezone.utc)
        
        text = f"""
{emoji} ЗАКРЫТИЕ СДЕЛКИ
Символ: {symbol}
Причина: {reason}
Вход: ${entry:.4f}
Выход: ${exit_price:.4f}
PnL Bybit, включая комиссии и funding: {sign}${pnl_usdt:.4f} ({sign}{pnl_percent:.1f}% от суммы входа)
Длительность: {duration}
Время (UTC): {local_time.strftime('%Y-%m-%d %H:%M:%S')}
"""
        self.send_message(text)
    
    def send_daily_stats(self, date: str, trades: int, profitable: int, 
                         pnl: float, balance: float, equity: float,
                         unrealised_pnl: float):
        if not self.enabled:
            return
        
        winrate = (profitable / trades * 100) if trades > 0 else 0
        sign = "+" if pnl > 0 else ""
        
        text = f"""
📊 ДНЕВНАЯ СТАТИСТИКА
Дата (UTC): {date}
Сделок: {trades}
Прибыльных: {profitable} ({winrate:.1f}%)
Подтвержденный PnL закрытых сделок: {sign}${pnl:.4f}
USDT wallet сейчас: {balance:.2f}
USDT equity сейчас: {equity:.2f}
Плавающий PnL сейчас: {unrealised_pnl:+.2f} USDT
Учет закрытий ведется с установки исправления.
"""
        self.send_message(text)
    
    def send_error(self, error_type: str, symbol: str, error_msg: str, retry: int = None):
        if not self.enabled:
            return
        
        retry_text = f"Повтор: {retry}/3" if retry is not None else ""
        
        text = f"""
⚠️ ОШИБКА
Тип: {error_type}
Символ: {symbol}
Ошибка: {error_msg}
{retry_text}
"""
        self.send_message(text)
