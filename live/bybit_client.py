import time
import logging
import math
from typing import Dict, Any, Optional, List
from pybit.unified_trading import HTTP
from tenacity import retry, stop_after_attempt, wait_exponential
from monitoring.runtime import observe_api_call
from performance.observations import observe_order_call

logger = logging.getLogger(__name__)

class BybitClient:
    """Обертка для Bybit API с retry и обработкой ошибок"""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.session = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret
        )
        self.rate_limit_remaining = 50
        self.rate_limit_reset = 0
        self.category = "linear"

    def _request(self, operation, *args, **kwargs):
        """Observe one existing SDK invocation without adding a retry or request."""
        return observe_order_call(getattr(self, 'performance', None), operation, kwargs.get("symbol", ""),
                                  observe_api_call, getattr(self, 'monitoring', None), operation,
                                  getattr(self.session, operation), *args, **kwargs)
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    def get_instruments(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Получить информацию об инструменте с retry"""
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
            
        response = self._request("get_instruments_info", **params)
        
        if response.get("retCode") != 0:
            raise RuntimeError(f"API Error: {response.get('retMsg')}")
            
        self._update_rate_limits(response)
        return response
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    def get_tickers(self) -> Dict[str, Any]:
        """Получить все тикеры"""
        response = self._request("get_tickers", category="linear")
        
        if response.get("retCode") != 0:
            raise RuntimeError(f"API Error: {response.get('retMsg')}")
            
        self._update_rate_limits(response)
        return response
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    def get_klines(self, symbol: str, interval: str, limit: int = 5) -> Dict[str, Any]:
        """Получить свечи"""
        response = self._request("get_kline",
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        
        if response.get("retCode") != 0:
            raise RuntimeError(f"API Error: {response.get('retMsg')}")
            
        self._update_rate_limits(response)
        return response
    
    def _update_rate_limits(self, response: Dict[str, Any]):
        """Обновить информацию о rate limits"""
        headers = getattr(response, 'headers', {})
        remaining = headers.get('X-Bapi-Limit-Status', '50')
        reset = headers.get('X-Bapi-Limit-Reset-Timestamp', '0')
        
        try:
            self.rate_limit_remaining = int(remaining)
            self.rate_limit_reset = int(reset)
        except:
            pass
    
    def wait_if_needed(self):
        """Подождать если близко rate limit"""
        if self.rate_limit_remaining < 5:
            wait_time = max(0, (self.rate_limit_reset / 1000) - time.time())
            if wait_time > 0:
                logger.warning(f"Rate limit low, waiting {wait_time:.1f}s")
                time.sleep(min(wait_time, 5))
    
    def get_wallet_balance(self, **kwargs) -> Dict[str, Any]:
        """Получить баланс кошелька"""
        response = self._request("get_wallet_balance", **kwargs)
        if response.get("retCode") != 0:
            raise RuntimeError(f"API Error: {response.get('retMsg')}")
        self._update_rate_limits(response)
        return response
    
    def place_order(self, **kwargs) -> Dict[str, Any]:
        """Разместить ордер"""
        response = self._request("place_order", **kwargs)
        if response.get("retCode") != 0:
            raise RuntimeError(f"API Error: {response.get('retMsg')}")
        self._update_rate_limits(response)
        return response
    
    def get_positions(self, symbol: str) -> List[Dict[str, Any]]:
        """An API failure must never look like an absent position."""
        response = self._request("get_positions", category=self.category, symbol=symbol)
        if response.get("retCode") != 0:
            raise RuntimeError(f"Position query failed for {symbol}: {response.get('retMsg')}")
        rows = response["result"]["list"]
        if not isinstance(rows, list):
            raise ValueError("Malformed position response")
        for row in rows:
            size = float(row["size"])
            if row["symbol"] != symbol or not math.isfinite(size) or size < 0:
                raise ValueError("Invalid position response")
            if size > 0 and row.get("side") not in ("Buy", "Sell"):
                raise ValueError("Unknown position side")
        return rows

    def get_entry_fill(self, symbol: str, order_id: str):
        response = self._request("get_order_history", category=self.category, symbol=symbol, orderId=order_id)
        if response.get("retCode") != 0:
            raise RuntimeError("Entry order query failed")
        for row in response['result']['list']:
            if row['orderId'] != order_id or row['symbol'] != symbol:
                raise ValueError("Entry order identity mismatch")
            if row['orderStatus'] not in ('Filled', 'Cancelled', 'PartiallyFilledCanceled', 'Rejected', 'Deactivated'):
                return None
            qty = float(row['cumExecQty'])
            price = float(row['avgPrice']) if qty else 0.0
            if not math.isfinite(qty) or qty < 0 or not math.isfinite(price) or (qty > 0 and price <= 0):
                raise ValueError("Invalid entry execution")
            return qty, price
        return None

    def get_closed_pnl(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """Read the full holding period in API-supported seven-day windows."""
        rows = []
        window_start = start_ms
        while window_start <= end_ms:
            window_end = min(end_ms, window_start + 7 * 24 * 60 * 60 * 1000 - 1)
            cursor = None
            seen_cursors = set()
            while True:
                params = dict(category=self.category, symbol=symbol, startTime=window_start,
                              endTime=window_end, limit=100)
                if cursor:
                    params["cursor"] = cursor
                response = self._request("get_closed_pnl", **params)
                if response.get("retCode") != 0:
                    raise RuntimeError(f"Closed PnL query failed: {response.get('retMsg')}")
                page = response["result"]["list"]
                if not isinstance(page, list):
                    raise ValueError("Malformed closed PnL response")
                rows.extend(page)
                cursor = response["result"].get("nextPageCursor")
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise ValueError("Repeated closed PnL cursor")
                seen_cursors.add(cursor)
            window_start = window_end + 1
        return rows
