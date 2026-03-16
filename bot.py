#!/usr/bin/env python3
"""
Bybit Short Strategy Bot
Стратегия: поиск пампивших монет (>=25% за 24ч) и вход в шорт при стагнации
Параметры: pump=0.25, tp=0.40, stall=4, sl=4.0
"""

import os
import sys
import time
import json
import logging
import signal
import math
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

# Создаем папку для логов
os.makedirs('data/logs', exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('data/logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Импорты наших модулей
from config import BotConfig
from live.bybit_client import BybitClient
from live.order_manager import OrderManager
from live.position_tracker import PositionTracker
from live.telegram_notifier import TelegramNotifier

# =====================================
# РИСК МЕНЕДЖЕР
# =====================================
# use the shared implementation located in live/risk_manager.py
from live.risk_manager import RiskManager

# (the imported class already tracks trades_history, daily limits, etc.)


# =====================================
# ОСНОВНОЙ БОТ
# =====================================
class ShortBot:
    def __init__(self):
        self.config = BotConfig()
        self.running = True

        self.client = BybitClient(
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
            testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true"
        )

        self.initial_balance = self.get_balance()
        self.risk_manager = RiskManager(initial_capital=self.initial_balance)
        self.tracker = PositionTracker(os.path.join(self.config.data_dir, "state.json"))
        self.notifier = TelegramNotifier(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=os.getenv("TELEGRAM_CHAT_ID")
        )

        self.last_bar_close = 0
        self._last_stall_check = 0

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        logger.info("=" * 60)
        logger.info("БОТ ЗАПУЩЕН")
        logger.info(f"Начальный баланс: ${self.initial_balance:.2f}")
        logger.info(f"Параметры: pump={self.config.pump_threshold*100}%, tp={self.config.tp_percent*100}%, stall={self.config.stall_bars}")
        logger.info("=" * 60)

    def get_balance(self) -> float:
        try:
            response = self.client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if response.get("retCode") == 0:
                return float(response["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
        return self.config.initial_capital

    def signal_handler(self, signum, frame):
        logger.info("Получен сигнал остановки, завершаем работу...")
        self.running = False

    def get_current_bar_close(self) -> int:
        now = int(time.time())
        return (now // (15 * 60)) * (15 * 60)

    def get_all_tickers(self) -> List[Dict[str, Any]]:
        try:
            response = self.client.get_tickers()
            return response["result"]["list"]
        except Exception as e:
            logger.error(f"Ошибка получения тикеров: {e}")
            return []

    def get_klines(self, symbol: str, max_retries: int = 3) -> Optional[List]:
        for attempt in range(max_retries):
            try:
                time.sleep(0.2)
                response = self.client.get_klines(
                    symbol=symbol,
                    interval="15",
                    limit=5
                )
                if response.get("retCode") == 10006:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Rate limit для {symbol}, ждем {wait_time}с")
                    time.sleep(wait_time)
                    continue
                if response.get("retCode") != 0:
                    logger.error(f"Ошибка API для {symbol}: {response.get('retMsg')}")
                    return None
                return response["result"]["list"]
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Не удалось получить свечи для {symbol}: {e}")
                    return None
                time.sleep((attempt + 1) * 2)
        return None

    def check_pump_candidate(self, ticker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            return None
        try:
            last = float(ticker.get("lastPrice", 0))
            high24 = float(ticker.get("highPrice24h", 0))
            low24 = float(ticker.get("lowPrice24h", 0))
            turnover = float(ticker.get("turnover24h", 0))
            if last <= 0 or high24 <= 0 or low24 <= 0:
                return None
            pump_pct = (last - low24) / low24
            near_high = last / high24
            if turnover < self.config.min_turnover_usdt:
                return None
            if pump_pct < self.config.pump_threshold:
                return None
            if near_high < self.config.near_high_ratio:
                return None
            return {
                "symbol": symbol,
                "last": last,
                "high24": high24,
                "low24": low24,
                "turnover": turnover,
                "pump_pct": pump_pct * 100,
                "near_high": near_high
            }
        except Exception as e:
            logger.error(f"Ошибка проверки {symbol}: {e}")
            return None

    def update_watchlist(self, candidates: List[Dict[str, Any]]):
        now = int(time.time())
        added = 0
        for cand in candidates:
            symbol = cand["symbol"]
            if self.tracker.in_cooldown(symbol, self.config.cooldown_minutes):
                continue
            if symbol in self.tracker.positions:
                continue
            if symbol in self.tracker.watchlist:
                continue
            klines = self.get_klines(symbol)
            if not klines or len(klines) < 2:
                continue
            last_candle = klines[-2]
            local_high = float(last_candle[2])
            self.tracker.watchlist[symbol] = {
                "local_high": local_high,
                "stall": 0,
                "blocked": False,
                "created_ts": now,
                "updated_ts": now,
                "entry_price": cand["last"]
            }
            added += 1
            logger.info(f"Добавлен в watchlist: {symbol} (памп {cand['pump_pct']:.1f}%)")
        if added:
            self.tracker.save()
            logger.info(f"Добавлено {added} новых монет в watchlist")

    def check_stall(self) -> List[Tuple[str, Dict[str, Any]]]:
        if time.time() - self._last_stall_check < 2:
            return []
        self._last_stall_check = time.time()
        ready = []
        now = int(time.time())
        for symbol, data in list(self.tracker.watchlist.items()):
            if now - data.get("created_ts", now) > self.config.watch_ttl_hours * 3600:
                logger.info(f"Удален из watchlist (TTL): {symbol}")
                del self.tracker.watchlist[symbol]
                continue
            klines = self.get_klines(symbol)
            if not klines:
                continue
            last_candle = klines[-2]
            high = float(last_candle[2])
            close = float(last_candle[4])
            local_high = data["local_high"]
            stall = data["stall"]
            blocked = data.get("blocked", False)
            if high > local_high:
                data["local_high"] = high
                data["stall"] = 0
                data["blocked"] = False
                logger.info(f"Новый локальный максимум для {symbol}: {high:.6f}")
            else:
                data["stall"] = stall + 1
            data["updated_ts"] = now
            if data["stall"] >= self.config.stall_bars and not blocked:
                tp_price = local_high * (1 - self.config.tp_percent)
                if close <= tp_price:
                    data["blocked"] = True
                    logger.info(f"⏭ {symbol}: цена уже ниже TP ({close:.6f} <= {tp_price:.6f}), блокируем")
                else:
                    ready.append((symbol, data))
        self.tracker.save()
        return ready

    def cancel_all_orders_for_symbol(self, symbol: str):
        try:
            response = self.client.session.get_open_orders(
                category=self.config.category,
                symbol=symbol
            )
            if response.get("retCode") != 0:
                return
            orders = response["result"]["list"]
            if not orders:
                return
            for order in orders:
                try:
                    self.client.session.cancel_order(
                        category=self.config.category,
                        symbol=symbol,
                        orderId=order["orderId"]
                    )
                except:
                    pass
        except:
            pass

    def reload_from_file(self):
        self.tracker.load()
        logger.info(f"Перезагружено из файла: {len(self.tracker.positions)} positions, {len(self.tracker.watchlist)} watchlist")

    def open_position(self, symbol: str, data: Dict[str, Any]):
        try:
            # общие предохранители
            if symbol in self.tracker.positions:
                logger.warning(f"⛔ Уже есть позиция по {symbol}, пропускаем")
                return

            if len(self.tracker.positions) >= self.config.max_concurrent_trades + 100:
                logger.warning("⛔ Достигнуто максимальное количество открытых позиций, пропускаем")
                return

            ok, reason = self.risk_manager.can_trade_today(datetime.now().date()) if hasattr(self.risk_manager, 'can_trade_today') else (True, '')
            if not ok:
                logger.warning(f"⛔ Пропуск открытия: {reason}")
                return

            # Проверка на бирже (чтобы избежать дубликатов)
            existing_positions = self.client.get_positions(symbol=symbol)
            if existing_positions and len(existing_positions) > 0 and float(existing_positions[0]['size']) > 0:
                logger.warning(f"⛔ На бирже уже есть позиция по {symbol} (размер: {existing_positions[0]['size']}), пропускаем")
                return

            # Баланс
            available = self.get_balance()
            position_usdt = self.config.base_risk_per_trade * self.risk_manager.current_capital
            multiplier = self.risk_manager.get_position_multiplier(symbol)
            position_usdt *= multiplier
            if available < position_usdt * 1.1:
                logger.error(f"❌ Недостаточно баланса: {available:.2f} < {position_usdt:.2f}")
                return
            
            # Цена
            tickers = self.get_all_tickers()
            current_price = None
            for t in tickers:
                if t["symbol"] == symbol:
                    current_price = float(t["lastPrice"])
                    break
            if not current_price:
                logger.error(f"❌ Не удалось получить цену для {symbol}")
                return
            
            # Расчет количества
            instr = self.client.get_instruments(symbol)
            filters = OrderManager.extract_filters(instr)
            qty = position_usdt / current_price
            qty_step = filters["qty_step"]
            if qty_step > 0:
                qty = math.floor(qty / qty_step) * qty_step
            
            min_qty = filters["min_qty"]
            qty = max(qty, min_qty)
            if qty * current_price < filters["min_notional"]:
                qty = math.ceil(filters["min_notional"] / current_price / qty_step) * qty_step
                logger.info(f"📊 Увеличили количество до {qty} для мин. суммы")
            
            # Форматирование qty (фикс invalid qty)
            precision = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
            str_qty = f"{qty:.{precision}f}".rstrip('0').rstrip('.') if '.' in f"{qty}" else f"{qty}"
            
            # TP/SL расчет
            local_high = data["local_high"]
            tp_price = local_high * (1 - self.config.tp_percent)
            sl_price = current_price * self.config.sl_multiplier
            tick_size = filters["tick_size"]
            tp_price = math.floor(tp_price / tick_size) * tick_size
            sl_price = math.floor(sl_price / tick_size) * tick_size
            
            # Отмена старых ордеров
            logger.info(f"🔄 Отменяем старые ордера для {symbol}")
            self.cancel_all_orders_for_symbol(symbol)
            
            # ОТКРЫТИЕ ПОЗИЦИИ С TP/SL
            logger.info(f"🚀 Открываем SHORT {symbol} по рынку, qty={str_qty}")
            response = self.client.place_order(
                category=self.config.category,
                symbol=symbol,
                side="Sell",
                orderType="Market",
                qty=str_qty,
                timeInForce="IOC",
                takeProfit=str(tp_price),
                stopLoss=str(sl_price),
                tpslMode="Full",
                tpTriggerBy="MarkPrice",
                slTriggerBy="MarkPrice",
                tpOrderType="Market",
                slOrderType="Market"
            )
            if response.get("retCode") != 0:
                logger.error(f"❌ Ошибка открытия: {response.get('retMsg')}")
                return
            
            # Получение реальных данных (фикс partial fill)
            time.sleep(1)
            position_info = self.client.get_positions(symbol=symbol)
            if position_info and len(position_info) > 0:
                actual_entry = float(position_info[0]['avgPrice'])
                actual_qty = float(position_info[0]['size'])
                logger.info(f"✅ Реальные данные: entry={actual_entry:.6f}, qty={actual_qty}")
            else:
                actual_entry = current_price
                actual_qty = qty
                logger.warning(f"⚠️ Не удалось получить позицию, используем расчетные данные")
            
            # Сохранение позиции
            position = {
                "symbol": symbol,
                "entry_price": actual_entry,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "qty": actual_qty,
                "open_time": int(time.time()),
                "multiplier": multiplier,
                "position_usdt": position_usdt,
                "local_high": local_high
            }
            self.tracker.add_position(position)
            
            # Удаление из watchlist
            if symbol in self.tracker.watchlist:
                del self.tracker.watchlist[symbol]
            self.tracker.save()
            
            # Уведомление
            self.notifier.send_trade_open(
                symbol, actual_entry, tp_price, sl_price,
                position_usdt, multiplier
            )
            logger.info(f"✅✅✅ ПОЗИЦИЯ ОТКРЫТА {symbol}")
            logger.info(f"   Вход: ${actual_entry:.6f}")
            logger.info(f"   TP: ${tp_price:.6f} ({(tp_price/actual_entry-1)*100:.1f}%)")
            logger.info(f"   SL: ${sl_price:.6f} ({self.config.sl_multiplier}x)")
            logger.info(f"   Размер: ${position_usdt:.2f} ({multiplier:.1f}x от 1%)")
            
        except Exception as e:
            logger.error(f"❌ Ошибка открытия позиции {symbol}: {e}", exc_info=True)

    def check_positions(self):
        for symbol, position in list(self.tracker.positions.items()):
            try:
                # Проверяем наличие на бирже
                position_info = self.client.get_positions(symbol)
                if not position_info or len(position_info) == 0 or float(position_info[0]['size']) == 0:
                    logger.info(f"📌 Позиция {symbol} закрылась на бирже, удаляем из трекера")

                    # уведомление/параметры
                    entry = position.get("entry_price", 0)

                    # если по API нет avgPrice, берём текущую цену тикера (лучше чем 0)
                    if position_info and len(position_info) > 0:
                        close_price = float(position_info[0].get('avgPrice', 0))
                    else:
                        close_price = 0
                        tick = next((t for t in self.get_all_tickers() if t["symbol"] == symbol), None)
                        if tick:
                            close_price = float(tick.get("lastPrice", 0))

                    pnl_usdt = (entry - close_price) * position.get("qty", 0)
                    pnl_percent = (entry - close_price) / entry * 100 if entry != 0 else 0

                    # уведомляем и обновляем статистику
                    self.notifier.send_trade_close(
                        symbol, entry, close_price, pnl_usdt, pnl_percent,
                        "TP/SL", "авто"
                    )

                    # расчёт риска
                    try:
                        self.risk_manager.on_trade_result(pnl_usdt, pnl_percent, symbol)
                    except Exception:
                        pass

                    # удаляем позицию и ставим отсчёт cooldown
                    self.tracker.remove_position(symbol)
                    continue

                # Проверка ключей
                required_keys = ['tp_price', 'sl_price', 'qty', 'entry_price']
                missing = [k for k in required_keys if k not in position]
                if missing:
                    logger.error(f"⚠️ Неполные данные позиции {symbol} (отсутствуют: {missing}), удаляем")
                    # используем helper чтобы установить cooldown
                    self.tracker.remove_position(symbol)
                    continue

            except Exception as e:
                logger.error(f"Ошибка проверки {symbol}: {e}")


    def run(self):
        self.reload_from_file()
        logger.info("Запуск основного цикла")
        
        # Для суточной статистики
        last_daily_report = datetime.now().date()
        
        while self.running:
            try:
                current_bar = self.get_current_bar_close()
                
                # Суточная статистика в 00:05
                today = datetime.now().date()
                if today != last_daily_report and datetime.now().hour == 0 and datetime.now().minute >= 5:
                    self.send_daily_stats()
                    last_daily_report = today
                
                if current_bar != self.last_bar_close:
                    self.last_bar_close = current_bar
                    logger.info(f"Новая свеча: {datetime.fromtimestamp(current_bar)}")
                    
                    tickers = self.get_all_tickers()
                    candidates = []
                    for t in tickers:
                        cand = self.check_pump_candidate(t)
                        if cand:
                            candidates.append(cand)
                    if candidates:
                        logger.info(f"Найдено кандидатов: {len(candidates)}")
                        self.update_watchlist(candidates)

                    ready = self.check_stall()
                    if ready:
                        logger.info(f"Готовы к входу: {len(ready)}")
                        for symbol, data in ready:
                            self.open_position(symbol, data)

                    self.check_positions()

                    logger.info(f"Статистика: watchlist={len(self.tracker.watchlist)}, "
                            f"positions={len(self.tracker.positions)}, "
                            f"balance=${self.risk_manager.current_capital:.2f}")

                time.sleep(self.config.wake_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Бот остановлен")
    
    def send_daily_stats(self):
        """Отправить суточную статистику"""
        try:
            # Собираем статистику за сегодня
            today = datetime.now().date()
            today_trades = [t for t in self.risk_manager.trades_history 
                        if datetime.fromisoformat(t['time']).date() == today]
            
            trades_count = len(today_trades)
            profitable = len([t for t in today_trades if t['pnl_usdt'] > 0])
            total_pnl = sum(t['pnl_usdt'] for t in today_trades)
            
            self.notifier.send_daily_stats(
                date=today.strftime('%Y-%m-%d'),
                trades=trades_count,
                profitable=profitable,
                pnl=total_pnl,
                balance=self.risk_manager.current_capital
            )
            logger.info(f"📊 Суточная статистика отправлена: сделок={trades_count}, PnL=${total_pnl:.2f}")
        except Exception as e:
            logger.error(f"Ошибка отправки суточной статистики: {e}")


def main():
    bot = ShortBot()
    bot.run()


if __name__ == "__main__":
    main()
