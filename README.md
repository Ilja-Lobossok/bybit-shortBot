# Bybit Short Strategy Bot

Trading bot for USDT-perpetual futures on Bybit.
Strategy: detect pumped coins (>=25% in 24h) and enter short on stall.

## Quick Start

1. Clone repository
2. cp .env.example .env and fill API keys
3. pip install -r requirements.txt
4. python bot.py

## Backtest Results (2025-2026)
- Return: +285%
- Win rate: 91.4%
- Trades: 2,198

## Configuration
See config.py

## Exchange accounting

New entries use a fresh Bybit Unified **USDT wallet balance**, with the existing
`base_risk_per_trade` and coin multiplier. The log separately reports USDT wallet,
equity, unrealized PnL, and available funds. This is not a strategy return: deposits,
withdrawals, fees, and funding can change the wallet. Borrowed USDT is excluded
from sizing. If the balance or the duplicate-position check fails, no new order is
sent. There is no fallback to an assumed $1000 balance.

A failed position request leaves its tracker record intact. Once the exchange
reports no open position, the bot waits for matching closed-PnL records covering
the full tracked quantity, including partial closes. It uses Bybit's `closedPnl`
and weighted execution prices, never zero `avgPrice` or a market-price estimate.
`closedPnl` already includes opening/closing fees and funding; they are not deducted
again. An ambiguous quantity or entry-price mismatch remains pending for inspection.
Trade history is paginated in seven-day windows across the entire holding period.

Position removal, cooldown, and verified risk history are saved together with
atomic file replacement. Accepted entry orders are recorded before querying their
fills, so a temporary query failure does not permit a second entry. Existing
`state.json` files keep their positions/watchlist/cooldowns when first upgraded.
Old fabricated profits are not imported: confirmed history and the wallet peak
start at the first successful balance query after this upgrade. The bot does not
reconstruct trades already removed by the previous version.

Daily Telegram reports cover the previous **UTC** date and include current wallet,
equity, and unrealized PnL separately from verified closed PnL. Daily limits and
coin statistics survive restarts. Close notifications are sent after saving the
state; a notification lost during a crash is not replayed as another accounting
event. Exchange history may lag, in which case the position remains pending until
the next 15-minute check. Manual changes to a tracked position can make matching
ambiguous and require inspection; no result is guessed.

The entry signal, candle selection, TP/SL parameters, and maximum-position setting
are unchanged by this accounting fix. Existing exchange orders are not modified
by deployment. Back up `DATA_DIR/state.json` before deploying and retain the
persistent disk. Do not run a second live worker against the same account.

### Offline regression tests

With dependencies installed, run:

```bash
python -m unittest tests.test_accounting -v
```

These tests mock all Bybit and Telegram calls and need no keys. `tests/test_api.py`
is a separate, live connectivity check and is not part of the offline suite.

API references: [wallet balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance),
[closed PnL](https://bybit-exchange.github.io/docs/v5/position/close-pnl),
[Bybit's PnL definitions](https://www.bybit.com/en/help-center/article/Profit-Loss-calculations-USDT-Contract).

## Structure
- ot.py - main bot file
- live/ - live trading modules
- acktest/ - backtester
- data/ - state and history (persistent disk)
