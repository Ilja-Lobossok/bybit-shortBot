"""Signed GET only, separate session, bounded pages/timeouts/rate. No trading SDK."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

DAY = 86_400_000
PATHS = {"wallet": "/v5/account/wallet-balance", "positions": "/v5/position/list",
         "executions": "/v5/execution/list", "orders": "/v5/order/history",
         "closed_pnl": "/v5/position/closed-pnl", "transactions": "/v5/account/transaction-log"}


class ReadError(RuntimeError):
    """Intentionally contains no response body, request, credential or exchange message."""


class Reader:
    def __init__(self, env, *, session=None, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.key = env.get("PERFORMANCE_BYBIT_API_KEY", "")
        self.secret = env.get("PERFORMANCE_BYBIT_API_SECRET", "")
        if not self.key or not self.secret:
            raise ReadError("Set separate performance read-only credentials")
        self.fingerprint = hashlib.sha256(self.key.encode()).hexdigest()
        self.base = "https://api-testnet.bybit.com" if env.get("PERFORMANCE_BYBIT_TESTNET", "false").lower() == "true" else "https://api.bybit.com"
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = False
        self.session = session
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.next_request = 0
        self.backoff = 0

    def get(self, kind, params):
        if kind not in PATHS:
            raise ReadError("Unsupported read operation")
        now = self.monotonic()
        if now < self.backoff:
            raise ReadError("Read collector backing off")
        # At most one GET per second, no immediate retries; all waits are on worker.
        self.sleep(max(0, self.next_request - now))
        self.next_request = self.monotonic() + 1
        query = urlencode(sorted((k, str(v)) for k, v in params.items()))
        stamp = str(int(self.clock() * 1000))
        signature = hmac.new(self.secret.encode(), (stamp + self.key + "5000" + query).encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": stamp,
                   "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN": signature}
        try:
            response = self.session.get(self.base + PATHS[kind] + "?" + query, headers=headers,
                                        timeout=(2, 5), allow_redirects=False, stream=True)
            with response:
                if response.status_code != 200:
                    raise ReadError("Read collector HTTP failure")
                # Never allocate an unbounded response body.
                content = bytearray()
                deadline = self.monotonic() + 10
                for chunk in response.iter_content(64 * 1024):
                    content.extend(chunk)
                    if len(content) > 2 * 1024**2 or self.monotonic() > deadline:
                        raise ReadError("Read collector response too large")
                import json
                value = json.loads(content)
                if value.get("retCode") != 0 or not isinstance(value.get("result", {}).get("list"), list):
                    raise ReadError("Read collector rejected or invalid response")
                remaining = int(response.headers.get("X-Bapi-Limit-Status", 50))
                if remaining < 5:
                    self.next_request = self.monotonic() + 60
                return value
        except Exception:
            self.backoff = self.monotonic() + 60
            raise ReadError("Read collector failed; details suppressed") from None

    def pages(self, kind, params):
        cursor, seen = None, set()
        for _ in range(100):
            response = self.get(kind, dict(params, **({"cursor": cursor} if cursor else {})))
            result = response["result"]
            following = result.get("nextPageCursor", "")
            if following and following in seen:
                raise ReadError("Repeated read cursor")
            yield result["list"], not bool(following), response
            if not following:
                return
            seen.add(following)
            cursor = following
        raise ReadError("Read page limit reached; window incomplete")

    def close(self):
        self.session.close()


class Collector:
    """One bounded history window per source per tick; restart uses committed watermarks."""
    KINDS = ("executions", "orders", "closed_pnl", "transactions")

    def __init__(self, reader, store, *, lookback_days=7):
        self.reader, self.store = reader, store
        self.lookback_days = lookback_days

    def snapshots(self, now):
        response = self.reader.get("wallet", {"accountType": "UNIFIED", "coin": "USDT"})
        self.store.snapshot(response, now)
        positions = []
        for rows, _final, _response in self.reader.pages("positions", {"category": "linear", "settleCoin": "USDT", "limit": 200}):
            positions.extend(rows)
            if len(positions) > 2000:
                raise ReadError("Position snapshot too large")
        self.store.replace_positions(positions, now)
        with self.store.db:
            self.store.set_meta("positions_acquisition_end_ms", int(self.reader.clock() * 1000))

    def history(self, now):
        # Two minute safety lag and 24h rolling overlap repair delayed/revised facts.
        end = now - 120_000
        origin = int(self.store.meta("history_origin_ms", max(0, end - self.lookback_days * DAY)))
        with self.store.db:
            self.store.set_meta("history_origin_ms", origin)
        for kind in self.KINDS:
            watermark = int(self.store.meta("watermark:" + kind, origin))
            start = max(origin, watermark - DAY)
            finish = min(end, start + 7 * DAY - 1)
            resume = self.store.db.execute("SELECT * FROM sync_cursors WHERE kind=?", (kind,)).fetchone()
            cursor = ""
            if resume:
                start, finish, cursor = resume["start_ms"], resume["end_ms"], resume["cursor"]
            if finish < start:
                continue
            params = {"startTime": start, "endTime": finish, "limit": 50 if kind in ("orders", "transactions") else 100}
            if kind == "transactions":
                params["accountType"] = "UNIFIED"  # no category filter: transfers must remain visible
            else:
                params.update(category="linear", settleCoin="USDT")
                if kind == "closed_pnl":
                    params.pop("settleCoin")  # endpoint does not accept settleCoin
            seen = {cursor} if cursor else set()
            for _ in range(4):
                try:
                    response = self.reader.get(kind, dict(params, **({"cursor": cursor} if cursor else {})))
                except ReadError:
                    # An expired cursor is not durable evidence. Replay the entire
                    # uncommitted window next time; already ingested IDs deduplicate.
                    with self.store.db:
                        self.store.db.execute("DELETE FROM sync_cursors WHERE kind=?", (kind,))
                    raise
                following = response["result"].get("nextPageCursor", "")
                if following in seen:
                    raise ReadError("Repeated history cursor; window incomplete")
                self.store.page(kind, start, finish, response["result"]["list"], now,
                                final=not bool(following), cursor=following)
                if not following:
                    break
                seen.add(following)
                cursor = following
