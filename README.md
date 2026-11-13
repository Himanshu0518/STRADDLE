# BTC ATM Short Straddle

A systematic options trading strategy built on [NautilusTrader](https://nautilustrader.io) connected to Bybit TestNet. Sells an at-the-money Call and Put on BTC options at the same strike and expiry, then monitors and closes the position automatically based on configurable risk thresholds.

---

## Strategy Overview

A **short straddle** collects premium by selling both sides of the market. The trader profits if BTC stays near the strike price — the combined premium decays as expiry approaches. The position loses money if BTC moves sharply in either direction, since the cost to buy back the options increases.

| Level | What it does |
|---|---|
| **Level 1** | Connect → load instruments → find ATM strike → sell Call + Put |
| **Level 2** | Monitor combined ask premium → auto-close on stop-loss or take-profit |

### Risk parameters (Level 2)

| Parameter | Default | Trigger |
|---|---|---|
| Stop-loss | 1% | Combined ask ≥ entry × 1.01 |
| Take-profit | 2% | Combined ask ≤ entry × 0.98 |

---

## Project Structure

```
straddle/
├── main.py           # Entry point — loads .env, builds node, runs
├── config.py         # ShortStraddleConfig + build_node_config()
├── atm_selector.py   # ATM strike selection (pure function, no strategy state)
├── strategy.py       # ShortStraddleStrategy — all event-driven logic
├── node.py           # TradingNode assembly
└── risk_manager.py   # check_exit_condition() — pure Python, no framework deps
```

---

## Execution Flow

```
1.  main() loads .env → reads API_KEY, API_SECRET

2.  build_node() assembles TradingNode:
    ├── BybitDataClientConfig  (TESTNET, OPTION, load_all BTC)
    └── BybitExecClientConfig  (TESTNET, OPTION, load_all BTC)

3.  node.build() connects clients:
    ├── InstrumentProvider loads all BTC options via HTTP (720 contracts)
    ├── WebSocket connects to Bybit option stream
    └── ExecEngine starts reconciliation

4.  strategy.on_start():
    ├── subscribe_instruments(BYBIT)
    └── subscribe_index_prices(BTCUSDT-LINEAR.BYBIT)

5.  on_index_price() fires (first tick only):
    ├── select_atm_straddle() scans cache:
    │   ├── Filter CryptoOption instruments, discard expired
    │   ├── Pick nearest expiry
    │   └── Find strike closest to BTC index price
    └── _submit_straddle():
        ├── SELL FOK 0.01 BTC CALL
        └── SELL FOK 0.01 BTC PUT

6.  Order lifecycle (per leg):
    SUBMITTED → ACCEPTED → FILLED
                         ↘ REJECTED / EXPIRED  → _handle_leg_failure()

7.  on_order_filled() (both legs):
    ├── Accumulate quantity-weighted entry premium
    ├── subscribe_quote_ticks(call), subscribe_quote_ticks(put)
    └── Log "STRADDLE LIVE"

8.  on_quote_tick() fires continuously:
    ├── Update latest ask for arriving leg
    ├── Compute combined ask premium (quantity-weighted)
    └── check_exit_condition() → STOP_LOSS | TAKE_PROFIT | None

9.  _trigger_exit():
    ├── BUY IOC call_qty CALL
    └── BUY IOC put_qty PUT

10. on_order_filled() (exit):
    └── Log "STRADDLE CLOSED" when all filled legs are bought back

11. Ctrl-C → on_stop():
    ├── cancel_all_orders() on both legs
    └── Emergency close if positions are still open

```

---

## Strategy 
```
on_start ─► wait for index price ─► on_index_price (ONE time)
                                         │ select ATM call+put
                                         ▼
                                   submit 2 SELL FOK orders
                                         │
              ┌──────────────────────────┼──────────────────────────┐
         both FILLED              one REJECTED/EXPIRED         both failed
              │                          │                          │
   compute entry premium        other leg filled? → buy it      reset, retry
   subscribe to quotes          back (avoid naked short)        next tick
              ▼
   on_quote_tick (many/sec) ─► combined ask vs entry premium
              │  SL or TP hit
              ▼
   _trigger_exit ─► BUY IOC both legs ─► fills ─► "STRADDLE CLOSED"

   ```

## Order Design Decisions

### Entry: FOK (Fill or Kill)

A partial fill on one leg creates a **naked short** — a completely different risk profile from a straddle. FOK guarantees either both legs are filled at the requested size, or neither is. If a FOK order is rejected or expires (zero fill), `_handle_leg_failure` detects whether the other leg is already short and immediately buys it back.

### Exit: IOC (Immediate or Cancel)

At close time, reducing exposure partially is better than not reducing it at all. If full size is unavailable, FOK on exit would leave the entire naked short open. IOC fills what it can. If an exit IOC order is itself rejected or expired, `on_order_rejected` / `on_order_expired` detect it via stored CLOIDs and re-attempt the close.

### Entry premium calculation

```
entry_premium = Σ(fill_price × fill_qty) / Σ(fill_qty)
```

Quantity-weighted to handle the case where a single order produces multiple fill events (Bybit can split fills). The combined ask premium at exit is computed on the same weighted basis.

### Stale quote prevention

`on_quote_tick` updates `_latest_call_ask` / `_latest_put_ask` directly from the **arriving tick**, not from `self.cache.quote_tick()`. This ensures the combined premium is never computed with a stale cross-leg price.

---

## Setup

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- Bybit TestNet account with a **Unified Trading Account** and API key with **Read-Write + Derivatives** permissions

### Install

```bash
uv sync
```

### Configure

Create a `.env` file in the project root:

```env
API_KEY=your_testnet_api_key
API_SECRET=your_testnet_api_secret
```

> **Note:** Use Bybit TestNet keys from [testnet.bybit.com](https://testnet.bybit.com), not mainnet. The strategy is hardcoded to `BybitEnvironment.TESTNET`.

### Run

```bash
uv run python main.py
```

Press `Ctrl+C` to stop. Open orders are cancelled and positions are closed on exit.

---

## Configuration

Risk parameters can be changed without modifying strategy code by updating `ShortStraddleConfig` in `node.py`:

```python
# node.py
ShortStraddleStrategy(config=ShortStraddleConfig(
    stop_loss_pct=0.015,   # 1.5%
    take_profit_pct=0.03,  # 3%
))
```

---

## Known Limitations

- **TestNet liquidity** — Bybit TestNet option books are thin. FOK orders may not fill. If this happens consistently, switching to aggressive limit orders (subscribing to quote ticks for entry) is the correct approach.
- **Emergency close is best-effort** — orders submitted in `on_stop` may not reach Bybit before `node.dispose()` tears down the connection.
- **Single expiry, minimum size** — the strategy always targets the nearest expiry at 0.01 BTC per leg (Bybit minimum). Extending to multiple expiries or larger sizes requires changes to `_submit_straddle` and ATM selection.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `nautilus-trader` | ≥1.231.0 | Trading engine + Bybit adapter |
| `python-dotenv` | ≥1.2.4 | Credential loading from `.env` |
