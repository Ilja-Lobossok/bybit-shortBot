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
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

from dotenv import load_dotenv
from monitoring.runtime import capture_environment, notify, start_monitoring
from performance.observations import capture_environment as capture_performance_environment, notify as performance_notify
from performance.runtime import start_performance

# Capture only real process env before the existing dotenv loader runs.
_MONITORING_ENV = capture_environment()
_PERFORMANCE_ENV = capture_performance_environment()

# Загружаем .env
load_dotenv()

# Создаем папку для логов
log_dir = os.path.join(os.getenv('DATA_DIR', './data'), 'logs')
os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, 'bot.log'), encoding='utf-8'),
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
from live.accounting import AccountBalance, ClosedTrade

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
        self.monitoring = start_monitoring(_MONITORING_ENV, self.config.data_dir)

        self.client = BybitClient(
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
            testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true"
        )
        self.client.monitoring = self.monitoring

        self.tracker = PositionTracker(os.path.join(self.config.data_dir, "state.json"))
        self.performance = start_performance(_PERFORMANCE_ENV, self.config.data_dir)
        self.client.performance = self.performance
        self._performance_bootstrap()
        self.account_balance = None
        self.risk_manager = None
        self.refresh_balance()
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
        logger.info("Баланс: %s", self.balance_summary())
        logger.info(f"Параметры: pump={self.config.pump_threshold*100}%, tp={self.config.tp_percent*100}%, stall={self.config.stall_bars}")
        logger.info("=" * 60)

    def _observe(self, method, *args, **kwargs):
        notify(getattr(self, 'monitoring', None), method, *args, **kwargs)

    def _performance(self, kind, **body):
        performance_notify(getattr(self, 'performance', None), kind, **body)

    def _performance_bootstrap(self):
        try:
            if not getattr(getattr(self, 'performance', None), 'enabled', False):
                return
            self._performance("bootstrap", entries=[
                {"symbol": symbol, "order_id": row["entry_order_id"]}
                for symbol, row in self.tracker.positions.items() if row.get("entry_order_id")])
        except Exception:
            pass

    def _performance_strategy(self, candidates, ready, scheduled_bar):
        try:
            if not getattr(getattr(self, 'performance', None), 'enabled', False):
                return
            rows = {row["symbol"]: dict(row, candidate=True) for row in candidates}
            for symbol, value in self.tracker.watchlist.items():
                rows.setdefault(symbol, {"symbol": symbol}).update(value, watching=True)
            for symbol, _value in ready:
                rows.setdefault(symbol, {"symbol": symbol})["ready"] = True
            self._performance("strategy_snapshot", rows=list(rows.values()), scheduled_bar=scheduled_bar)
        except Exception:
            pass

    def refresh_balance(self) -> bool:
        """Invalidate stale balances on failure; only exchange amounts allow entries."""
        self.account_balance = None
        try:
            response = self.client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            balance = AccountBalance.from_response(response)
            if self.risk_manager is None:
                self.risk_manager = RiskManager.from_state(self.tracker.risk_state, balance.wallet)
            else:
                self.risk_manager.sync_capital(balance.wallet)
            self.tracker.risk_state = self.risk_manager.to_state()
            self.tracker.save()
            self.account_balance = balance
            self._observe("balance_validated")
            return True
        except Exception as e:
            self._observe("fail_cycle")
            logger.error("Баланс не подтвержден, новые входы запрещены: %s", e)
            return False

    def balance_summary(self):
        if self.account_balance is None:
            return "UNAVAILABLE (новые входы запрещены)"
        balance = self.account_balance
        return (f"USDT wallet={balance.wallet:.2f}, equity={balance.equity:.2f}, "
                f"unrealised_pnl={balance.unrealised_pnl:+.2f}, available={balance.available:.2f}")

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
            self._observe("fail_cycle")
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
                    self._observe("fail_cycle")
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Rate limit для {symbol}, ждем {wait_time}с")
                    time.sleep(wait_time)
                    continue
                if response.get("retCode") != 0:
                    self._observe("fail_cycle")
                    logger.error(f"Ошибка API для {symbol}: {response.get('retMsg')}")
                    return None
                return response["result"]["list"]
            except Exception as e:
                self._observe("fail_cycle")
                if attempt == max_retries - 1:
                    logger.error(f"Не удалось получить свечи для {symbol}: {e}")
                    return None
                time.sleep((attempt + 1) * 2)
        return None

    def check_pump_candidate(self, ticker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            self._performance("decision", symbol=symbol, reason="not_usdt")
            return None
        try:
            last = float(ticker.get("lastPrice", 0))
            high24 = float(ticker.get("highPrice24h", 0))
            low24 = float(ticker.get("lowPrice24h", 0))
            turnover = float(ticker.get("turnover24h", 0))
            if last <= 0 or high24 <= 0 or low24 <= 0:
                self._performance("decision", symbol=symbol, reason="invalid_market")
                return None
            pump_pct = (last - low24) / low24
            near_high = last / high24
            if turnover < self.config.min_turnover_usdt:
                self._performance("decision", symbol=symbol, reason="turnover")
                return None
            if pump_pct < self.config.pump_threshold:
                self._performance("decision", symbol=symbol, reason="pump")
                return None
            if near_high < self.config.near_high_ratio:
                self._performance("decision", symbol=symbol, reason="near_high")
                return None
            self._performance("decision", symbol=symbol, reason="candidate", pump_pct=pump_pct * 100)
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
            self._observe("fail_cycle")
            logger.error(f"Ошибка проверки {symbol}: {e}")
            return None

    def update_watchlist(self, candidates: List[Dict[str, Any]]):
        now = int(time.time())
        added = 0
        for cand in candidates:
            symbol = cand["symbol"]
            if self.tracker.in_cooldown(symbol, self.config.cooldown_minutes):
                self._performance("decision", symbol=symbol, reason="cooldown")
                continue
            if symbol in self.tracker.positions:
                self._performance("decision", symbol=symbol, reason="tracked")
                continue
            if symbol in self.tracker.watchlist:
                self._performance("decision", symbol=symbol, reason="watching")
                continue
            klines = self.get_klines(symbol)
            if not klines or len(klines) < 2:
                self._performance("decision", symbol=symbol, reason="missing_klines")
                self._observe("fail_cycle")
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
                self._performance("decision", symbol=symbol, reason="ttl")
                logger.info(f"Удален из watchlist (TTL): {symbol}")
                del self.tracker.watchlist[symbol]
                continue
            klines = self.get_klines(symbol)
            if not klines:
                self._performance("decision", symbol=symbol, reason="missing_klines")
                self._observe("fail_cycle")
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
                self._performance("decision", symbol=symbol, reason="new_high", candle_ms=last_candle[0], local_high=high)
                logger.info(f"Новый локальный максимум для {symbol}: {high:.6f}")
            else:
                data["stall"] = stall + 1
                self._performance("decision", symbol=symbol, reason="stall", stall=data["stall"], candle_ms=last_candle[0])
            data["updated_ts"] = now
            if data["stall"] >= self.config.stall_bars and not blocked:
                tp_price = local_high * (1 - self.config.tp_percent)
                if close <= tp_price:
                    self._performance("decision", symbol=symbol, reason="below_tp", candle_ms=last_candle[0])
                    data["blocked"] = True
                    logger.info(f"⏭ {symbol}: цена уже ниже TP ({close:.6f} <= {tp_price:.6f}), блокируем")
                else:
                    ready.append((symbol, data))
                    self._performance("decision", symbol=symbol, reason="ready", stall=data["stall"], candle_ms=last_candle[0])
        self.tracker.save()
        return ready

    def cancel_all_orders_for_symbol(self, symbol: str):
        try:
            response = self.client._request("get_open_orders",
                category=self.config.category,
                symbol=symbol
            )
            if response.get("retCode") != 0:
                self._observe("fail_cycle")
                return
            orders = response["result"]["list"]
            if not orders:
                return
            for order in orders:
                try:
                    self.client._request("cancel_order",
                        category=self.config.category,
                        symbol=symbol,
                        orderId=order["orderId"]
                    )
                except:
                    self._observe("fail_cycle")
                    pass
        except:
            self._observe("fail_cycle")
            pass

    def reload_from_file(self):
        self.tracker.load()
        self._performance_bootstrap()
        self.risk_manager = None
        self.refresh_balance()
        logger.info(f"Перезагружено из файла: {len(self.tracker.positions)} positions, {len(self.tracker.watchlist)} watchlist")

    def open_position(self, symbol: str, data: Dict[str, Any]):
        try:
            # общие предохранители
            if symbol in self.tracker.positions:
                self._performance("decision", symbol=symbol, reason="tracked")
                logger.warning(f"⛔ Уже есть позиция по {symbol}, пропускаем")
                return

            if len(self.tracker.positions) >= self.config.max_concurrent_trades + 100:
                self._performance("decision", symbol=symbol, reason="position_limit")
                logger.warning("⛔ Достигнуто максимальное количество открытых позиций, пропускаем")
                return

            if not self.refresh_balance():
                self._performance("decision", symbol=symbol, reason="balance")
                return
            ok, reason = self.risk_manager.can_trade_today(datetime.now(timezone.utc).date())
            if not ok:
                self._performance("decision", symbol=symbol, reason="risk", risk_detail=reason)
                logger.warning(f"⛔ Пропуск открытия: {reason}")
                return

            # Проверка на бирже (чтобы избежать дубликатов)
            existing_positions = self.client.get_positions(symbol=symbol)
            if any(float(row['size']) > 0 for row in existing_positions):
                self._performance("decision", symbol=symbol, reason="exchange_position")
                logger.warning(f"⛔ На бирже уже есть позиция по {symbol}, пропускаем")
                return

            # Баланс
            available = self.account_balance.available
            position_usdt = self.config.base_risk_per_trade * self.account_balance.wallet
            multiplier = self.risk_manager.get_position_multiplier(symbol)
            position_usdt *= multiplier
            if available < position_usdt * 1.1:
                self._performance("decision", symbol=symbol, reason="funds")
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
                self._performance("decision", symbol=symbol, reason="missing_price")
                self._observe("fail_cycle")
                logger.error(f"❌ Не удалось получить цену для {symbol}")
                return
            
            # Расчет количества
            instr = self.client.get_instruments(symbol)
            filters = OrderManager.extract_filters(instr)
            qty = position_usdt / current_price
            qty_step = filters["qty_step"]
            if qty_step > 0:
                step = Decimal(str(qty_step))
                qty = float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)
            
            min_qty = filters["min_qty"]
            qty = max(qty, min_qty)
            if qty * current_price < filters["min_notional"]:
                qty = math.ceil(filters["min_notional"] / current_price / qty_step) * qty_step
                logger.info(f"📊 Увеличили количество до {qty} для мин. суммы")
            if qty * current_price * 1.1 > available:
                self._performance("decision", symbol=symbol, reason="min_order")
                logger.warning("Минимальный ордер %s превышает доступный баланс", symbol)
                return
            
            # Форматирование qty (фикс invalid qty)
            str_qty = format(Decimal(str(qty)).normalize(), 'f')
            
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
            opened_at_ms = int(time.time() * 1000)
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
                self._observe("fail_cycle")
                logger.error(f"❌ Ошибка открытия: {response.get('retMsg')}")
                return
            
            # Persist the accepted order before another API request can fail.
            # The provisional record blocks a duplicate entry until reconciled.
            
            # Сохранение позиции
            position = {
                "symbol": symbol,
                "entry_price": current_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "qty": qty,
                "open_time": opened_at_ms // 1000,
                "opened_at_ms": opened_at_ms,
                "entry_order_id": response["result"]["orderId"],
                "entry_pending": True,
                "multiplier": multiplier,
                "position_usdt": position_usdt,
                "local_high": local_high
            }
            self.tracker.add_position(position)
            self._performance("entry", symbol=symbol, order_id=position["entry_order_id"], flat_before=True)
            self._performance("decision", symbol=symbol, reason="accepted")

            time.sleep(1)
            position_info = self.client.get_positions(symbol=symbol)
            active_short = next((row for row in position_info if row.get('side') == 'Sell'
                                 and float(row['size']) > 0), None)
            if active_short is None:
                self._performance("decision", symbol=symbol, reason="pending")
                self._observe("fail_cycle")
                logger.warning("Ожидаем подтверждения исполнения %s", symbol)
                return
            actual_entry = float(active_short['avgPrice'])
            actual_qty = float(active_short['size'])
            if not math.isfinite(actual_entry) or actual_entry <= 0:
                raise ValueError("Invalid executed entry price")
            position.update(entry_price=actual_entry, qty=actual_qty, entry_pending=False)
            self.tracker.save()
            logger.info(f"✅ Реальные данные: entry={actual_entry:.6f}, qty={actual_qty}")
            
            # Удаление из watchlist
            if symbol in self.tracker.watchlist:
                del self.tracker.watchlist[symbol]
            self.tracker.save()
            
            # Уведомление
            self.notifier.send_trade_open(
                symbol, actual_entry, tp_price, sl_price,
                actual_qty * actual_entry, multiplier
            )
            logger.info(f"✅✅✅ ПОЗИЦИЯ ОТКРЫТА {symbol}")
            logger.info(f"   Вход: ${actual_entry:.6f}")
            logger.info(f"   TP: ${tp_price:.6f} ({(tp_price/actual_entry-1)*100:.1f}%)")
            logger.info(f"   SL: ${sl_price:.6f} ({self.config.sl_multiplier}x)")
            logger.info(f"   Фактическая сумма входа: ${actual_qty * actual_entry:.2f}")
            
        except Exception as e:
            self._observe("fail_cycle")
            logger.error(f"❌ Ошибка открытия позиции {symbol}: {e}", exc_info=True)

    def check_positions(self):
        for symbol, position in list(self.tracker.positions.items()):
            try:
                position_info = self.client.get_positions(symbol)
                active = [row for row in position_info if float(row['size']) > 0]
                if position.get('entry_pending'):
                    fill = self.client.get_entry_fill(symbol, position['entry_order_id'])
                    if fill is None:
                        self._observe("fail_cycle")
                        continue
                    actual_qty, actual_entry = fill
                    if actual_qty == 0:
                        self.tracker.remove_position(symbol)  # IOC cancelled without fills.
                        continue
                    position.update(qty=actual_qty, entry_price=actual_entry, entry_pending=False)
                    self.tracker.watchlist.pop(symbol, None)
                    self.tracker.save()
                    self.notifier.send_trade_open(symbol, actual_entry, position['tp_price'],
                                                  position['sl_price'], actual_qty * actual_entry,
                                                  position['multiplier'])
                if active:
                    continue
                # An absent position does not yet prove its closing price or PnL.
                # Legacy open_time was saved after the fill, so allow 60s of skew.
                start_ms = position.get('opened_at_ms', (int(position['open_time']) - 60) * 1000)
                end_ms = int(time.time() * 1000)
                records = self.client.get_closed_pnl(symbol, start_ms, end_ms)
                trade = ClosedTrade.from_records(symbol, position, records, start_ms, end_ms)
                if trade is None:
                    self._observe("fail_cycle")
                    logger.warning("Ожидаем полную историю закрытия %s; запись сохранена", symbol)
                    continue
                if self.risk_manager is None:
                    self._observe("fail_cycle")
                    logger.warning("Учет закрытия %s отложен до подтверждения баланса", symbol)
                    continue
                # Persist accounting and removal in one atomic state replacement.
                updated_risk = deepcopy(self.risk_manager)
                updated_risk.on_trade_result(
                    trade.pnl, trade.pnl_percent, symbol,
                    datetime.fromtimestamp(trade.closed_at_ms / 1000, timezone.utc), trade.order_ids,
                )
                self.tracker.close_position(symbol, updated_risk.to_state())
                self.risk_manager = updated_risk
                duration = max(0, trade.closed_at_ms // 1000 - int(position['open_time']))
                self.notifier.send_trade_close(
                    symbol, trade.entry, trade.exit, trade.pnl, trade.pnl_percent,
                    "Подтверждено Bybit (closed PnL)", f"{duration // 60} мин",
                )
                logger.info("Закрытие подтверждено Bybit: %s exit=%.8f net_pnl=%+.4f USDT",
                            symbol, trade.exit, trade.pnl)
            except Exception as e:
                self._observe("fail_cycle")
                logger.error("Проверка %s не завершена; позиция сохранена: %s", symbol, e)


    def run(self):
        self.reload_from_file()
        logger.info("Запуск основного цикла")
        
        # Для суточной статистики
        last_daily_report = datetime.now(timezone.utc).date()
        
        while self.running:
            try:
                self._observe("progress")
                current_bar = self.get_current_bar_close()
                
                # Суточная статистика в 00:05
                today = datetime.now(timezone.utc).date()
                if today != last_daily_report and datetime.now(timezone.utc).minute >= 5:
                    self.send_daily_stats()
                    last_daily_report = today
                
                if current_bar != self.last_bar_close:
                    self.last_bar_close = current_bar
                    logger.info(f"Новая свеча: {datetime.fromtimestamp(current_bar)}")
                    self._observe("begin_cycle")
                    
                    # Reconcile closures before evaluating today's entry limits.
                    balance_before_ok = self.refresh_balance()
                    self._observe("stage_completed", "balance_before", balance_before_ok)
                    self.check_positions()
                    self._observe("stage_completed", "positions")
                    tickers = self.get_all_tickers()
                    self._observe("stage_completed", "tickers", bool(tickers))
                    candidates = []
                    for t in tickers:
                        cand = self.check_pump_candidate(t)
                        if cand:
                            candidates.append(cand)
                    if candidates:
                        logger.info(f"Найдено кандидатов: {len(candidates)}")
                        self.update_watchlist(candidates)
                    self._observe("stage_completed", "watchlist")

                    ready = self.check_stall()
                    self._observe("stage_completed", "stall")
                    if ready:
                        logger.info(f"Готовы к входу: {len(ready)}")
                        for symbol, data in ready:
                            self.open_position(symbol, data)
                    self._observe("stage_completed", "entries")
                    self._performance_strategy(candidates, ready, current_bar)

                    balance_after_ok = self.refresh_balance()
                    self._observe("stage_completed", "balance_after", balance_after_ok)

                    logger.info(f"Статистика: watchlist={len(self.tracker.watchlist)}, "
                            f"positions={len(self.tracker.positions)}, "
                            f"{self.balance_summary()}")
                    self._observe("finish_cycle")

                time.sleep(self.config.wake_seconds)

            except KeyboardInterrupt:
                self._observe("fail_cycle")
                break
            except Exception as e:
                self._observe("fail_cycle")
                logger.error(f"Ошибка в основном цикле: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Бот остановлен")
        self._observe("stop")
        try:
            observer = getattr(self, 'performance', None)
            if observer is not None and observer.enabled:
                observer.stop()
        except Exception:
            pass
    
    def send_daily_stats(self):
        """Отправить суточную статистику"""
        try:
            # Собираем статистику за сегодня
            if not self.refresh_balance():
                return
            today = datetime.now(timezone.utc).date() - timedelta(days=1)
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
                balance=self.account_balance.wallet,
                equity=self.account_balance.equity,
                unrealised_pnl=self.account_balance.unrealised_pnl,
            )
            logger.info(f"📊 Суточная статистика отправлена: сделок={trades_count}, PnL=${total_pnl:.2f}")
        except Exception as e:
            logger.error(f"Ошибка отправки суточной статистики: {e}")


def main():
    bot = ShortBot()
    bot.run()


if __name__ == "__main__":
    main()
