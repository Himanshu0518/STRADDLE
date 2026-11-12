"""
BTC ATM Short Straddle — Level 1
=================================
NautilusTrader 1.231.0 + Bybit TestNet

Execution flow:
  1. TradingNode starts; Bybit adapter connects to TestNet.
  2. InstrumentProvider loads all BTC option instruments (HTTP) before on_start fires.
  3. Strategy subscribes to BTC index price via BTCUSDT-LINEAR channel.
  4. On first IndexPriceUpdate, the nearest-expiry ATM strike is identified by scanning
     the instrument cache (CryptoOption objects), comparing strike_price to BTC index price.
  5. The closest-strike Call + Put pair is selected.
  6. Two SELL market orders (quantity = minimum lot = 0.01 BTC) are submitted — one for
     each leg.
  7. Fill events are tracked via on_order_filled; on_position_opened logs open positions.
  8. A one-shot guard (self._orders_submitted) prevents duplicate order submissions on
     subsequent index price ticks.
"""

from __future__ import annotations

import os
from decimal import Decimal

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# NautilusTrader — Bybit adapter
# ---------------------------------------------------------------------------
from nautilus_trader.adapters.bybit import (
    BybitDataClientConfig,
    BybitExecClientConfig,
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)

# ---------------------------------------------------------------------------
# NautilusTrader — config
# ---------------------------------------------------------------------------
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    StrategyConfig,
    TradingNodeConfig,
)

# ---------------------------------------------------------------------------
# NautilusTrader — Bybit enums (from pyo3 layer)
# ---------------------------------------------------------------------------
from nautilus_trader.core.nautilus_pyo3 import BybitEnvironment, BybitProductType

# ---------------------------------------------------------------------------
# NautilusTrader — live node
# ---------------------------------------------------------------------------
from nautilus_trader.live.node import TradingNode

# ---------------------------------------------------------------------------
# NautilusTrader — model
# ---------------------------------------------------------------------------
from nautilus_trader.model.data import IndexPriceUpdate
from nautilus_trader.model.enums import OptionKind, OrderSide, TimeInForce
from nautilus_trader.model.events.order import OrderFilled
from nautilus_trader.model.events.position import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import CryptoOption

# ---------------------------------------------------------------------------
# NautilusTrader — strategy base
# ---------------------------------------------------------------------------
from nautilus_trader.trading.strategy import Strategy


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------

class ShortStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for ShortStraddleStrategy.

    No extra parameters needed for Level 1 — all runtime state lives in the
    Strategy instance itself. Declaring `frozen=True` satisfies msgspec's
    requirement that StrategyConfig subclasses are immutable.
    """
    pass


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class ShortStraddleStrategy(Strategy):
    """
    Level-1 BTC ATM Short Straddle on Bybit TestNet.

    Lifecycle
    ---------
    on_start          → subscribe to instruments + BTC index price
    on_index_price    → (once) identify ATM strike, submit SELL orders
    on_order_filled   → log fills; detect when both legs are done
    on_position_opened→ log newly opened positions
    on_stop           → cancel any residual open orders
    """

    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)

        # --- one-shot guards ---
        self._orders_submitted: bool = False

        # --- selected leg instrument IDs (set when ATM pair found) ---
        self._call_id: InstrumentId | None = None
        self._put_id: InstrumentId | None = None

        # --- fill tracking ---
        self._call_filled: bool = False
        self._put_filled: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """
        Called by NautilusTrader after all clients have connected and the
        instrument provider has populated the cache.

        We subscribe to:
          - instrument updates (keeps cache fresh on new listings)
          - BTC index price ticks (triggers ATM selection on first tick)
        """
        # Keep the instrument cache up to date as new contracts list
        self.subscribe_instruments(venue=Venue("BYBIT"))

        # BTC index price comes via the LINEAR product ticker channel
        btc_linear_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
        self.subscribe_index_prices(instrument_id=btc_linear_id)

        self._log.info("Strategy started — waiting for BTC index price tick")

    def on_stop(self) -> None:
        """Cancel any open orders on both legs before the node shuts down."""
        if self._call_id is not None:
            self.cancel_all_orders(self._call_id)
        if self._put_id is not None:
            self.cancel_all_orders(self._put_id)
        self._log.info("Strategy stopped — open orders cancelled (if any)")

    # ------------------------------------------------------------------
    # Index price callback — triggers one-shot straddle submission
    # ------------------------------------------------------------------

    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        """
        Fires on every BTC index price tick.  The first time it fires we
        select the ATM straddle and submit orders; subsequent ticks are
        ignored via the _orders_submitted guard.
        """
        # Guard: only act once
        if self._orders_submitted:
            return

        btc_price = float(index_price.value)
        self._log.info(f"BTC index price: {btc_price:.2f}")

        # Try to build the ATM pair from the cache
        result = self._select_atm_straddle(btc_price)
        if result is None:
            # Cache may still be warming up — will retry on next tick
            self._log.warning("ATM straddle selection failed; will retry on next index price tick")
            return

        call, put = result
        self._call_id = call.id
        self._put_id = put.id

        # Set flag BEFORE submitting to prevent race if on_index_price fires
        # again before the first submit_order returns
        self._orders_submitted = True

        self._submit_straddle(call, put)

    # ------------------------------------------------------------------
    # ATM selection helper
    # ------------------------------------------------------------------

    def _select_atm_straddle(
        self, index_price: float
    ) -> tuple[CryptoOption, CryptoOption] | None:
        """
        Scan the instrument cache for BTC options and return the (Call, Put)
        pair at the nearest-expiry ATM strike.

        Steps:
          1. Pull all CryptoOption instruments from the BYBIT venue cache.
          2. Discard expired contracts.
          3. Take the soonest expiry.
          4. Among that expiry's contracts find the strike closest to index_price.
          5. Return the (CALL, PUT) pair at that strike.

        Returns None if any step cannot complete (empty cache, no future expiries,
        missing leg at strike).
        """
        import datetime  # local import keeps module-level namespace tidy

        # 1. All cached BTC option instruments for BYBIT
        all_instruments = self.cache.instruments(venue=Venue("BYBIT"))
        options = [i for i in all_instruments if isinstance(i, CryptoOption)]

        if not options:
            self._log.error(
                "No BTC options found in cache — verify InstrumentProviderConfig "
                "has load_all=True and filters={'base_coin': 'BTC'}"
            )
            return None

        self._log.info(f"Cache contains {len(options)} BTC option instruments")

        # 2. Keep only unexpired contracts
        now_ns = self.clock.timestamp_ns()
        future_opts = [o for o in options if o.expiration_ns > now_ns]

        if not future_opts:
            self._log.error("All cached BTC options have already expired")
            return None

        # 3. Nearest expiry (smallest expiration_ns that is in the future)
        nearest_expiry_ns = min(o.expiration_ns for o in future_opts)
        expiry_opts = [o for o in future_opts if o.expiration_ns == nearest_expiry_ns]

        expiry_dt = datetime.datetime.fromtimestamp(
            nearest_expiry_ns / 1e9, tz=datetime.timezone.utc
        )
        self._log.info(
            f"Nearest expiry: {expiry_dt.date()} "
            f"({len(expiry_opts)} contracts at this expiry)"
        )

        # 4. Strike closest to index price
        strikes = sorted({float(o.strike_price) for o in expiry_opts})
        if not strikes:
            self._log.error("No strikes found for nearest expiry")
            return None

        atm_strike = min(strikes, key=lambda s: abs(s - index_price))
        self._log.info(
            f"ATM strike selected: {atm_strike} "
            f"(BTC index: {index_price:.2f}, delta: {abs(atm_strike - index_price):.2f})"
        )

        # 5. Find CALL and PUT at that strike (use 1.0 tolerance for float comparison)
        atm_opts = [
            o for o in expiry_opts
            if abs(float(o.strike_price) - atm_strike) < 1.0
        ]
        call = next((o for o in atm_opts if o.option_kind == OptionKind.CALL), None)
        put = next((o for o in atm_opts if o.option_kind == OptionKind.PUT), None)

        if call is None or put is None:
            self._log.error(
                f"Could not find both legs at ATM strike {atm_strike}. "
                f"Found: {[o.id.symbol.value for o in atm_opts]}"
            )
            return None

        self._log.info(
            f"ATM straddle selected — "
            f"CALL: {call.id}  PUT: {put.id}  expiry: {expiry_dt.date()}"
        )
        return call, put

    # ------------------------------------------------------------------
    # Order submission helper
    # ------------------------------------------------------------------

    def _submit_straddle(self, call: CryptoOption, put: CryptoOption) -> None:
        """
        Submit two SELL market orders — one for the call leg, one for the put
        leg — using the instrument's minimum lot size (0.01 BTC).

        make_qty() quantises the Decimal to the instrument's size_precision so
        the execution client won't reject it for precision mismatch.
        """
        try:
            # Use minimum lot size; make_qty respects the instrument's size precision
            call_qty = call.make_qty(Decimal("0.01"))
            put_qty = put.make_qty(Decimal("0.01"))

            # Build SELL market order for the call leg
            call_order = self.order_factory.market(
                instrument_id=call.id,
                order_side=OrderSide.SELL,
                quantity=call_qty,
                time_in_force=TimeInForce.GTC,
            )

            # Build SELL market order for the put leg
            put_order = self.order_factory.market(
                instrument_id=put.id,
                order_side=OrderSide.SELL,
                quantity=put_qty,
                time_in_force=TimeInForce.GTC,
            )

            self._log.info(f"Submitting SELL {call_qty} CALL: {call.id}")
            self.submit_order(call_order)

            self._log.info(f"Submitting SELL {put_qty} PUT: {put.id}")
            self.submit_order(put_order)

        except Exception as exc:
            # Log the error but do NOT reset _orders_submitted — a retry
            # would likely fail for the same reason and produce duplicate orders.
            self._log.error(f"Order submission failed: {exc!r}")

    # ------------------------------------------------------------------
    # Fill / position callbacks
    # ------------------------------------------------------------------

    def on_order_filled(self, event: OrderFilled) -> None:
        """Log each fill and detect when the full straddle is complete."""
        self._log.info(
            f"ORDER FILLED: {event.instrument_id} "
            f"side={event.order_side} "
            f"qty={event.last_qty} "
            f"px={event.last_px} "
            f"commission={event.commission}"
        )

        # Track individual leg fills
        if event.instrument_id == self._call_id:
            self._call_filled = True
        elif event.instrument_id == self._put_id:
            self._put_filled = True

        # Announce when both legs are live
        if self._call_filled and self._put_filled:
            self._log.info(
                "=== BOTH LEGS FILLED — Short straddle is LIVE! "
                f"CALL={self._call_id}  PUT={self._put_id} ==="
            )

    def on_position_opened(self, event: PositionOpened) -> None:
        """Log each newly opened position."""
        self._log.info(
            f"POSITION OPENED: {event.instrument_id} "
            f"side={event.position_side} "
            f"qty={event.quantity} "
            f"avg_px={event.avg_px_open}"
        )


# ---------------------------------------------------------------------------
# Node setup + entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Build and run the TradingNode.

    Configuration:
      - Credentials loaded from .env (API_KEY, API_SECRET)
      - Bybit TestNet, OPTION product type, BTC instruments only
      - load_all=True so the cache is populated before on_start fires
    """
    # --- Load credentials from .env ---
    load_dotenv()
    api_key = os.environ["API_KEY"]
    api_secret = os.environ["API_SECRET"]

    # --- Instrument provider: load all BTC options on connect ---
    instrument_cfg = InstrumentProviderConfig(
        load_all=True,
        filters={"base_coin": "BTC"},
    )

    # --- Bybit data client (market data, instrument loading, WS feeds) ---
    data_client_cfg = BybitDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        product_types=(BybitProductType.OPTION,),
        environment=BybitEnvironment.TESTNET,
        instrument_provider=instrument_cfg,
    )

    # --- Bybit execution client (order management, fills, positions) ---
    exec_client_cfg = BybitExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        product_types=(BybitProductType.OPTION,),
        environment=BybitEnvironment.TESTNET,
        instrument_provider=instrument_cfg,
    )

    # --- Trading node configuration ---
    # Note: LiveDataEngineConfig does NOT have a reconciliation field;
    #       reconciliation belongs only to LiveExecEngineConfig.
    node_config = TradingNodeConfig(
        trader_id="TRADER-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={"BYBIT": data_client_cfg},
        exec_clients={"BYBIT": exec_client_cfg},
        exec_engine=LiveExecEngineConfig(reconciliation=True),
    )

    # --- Instantiate strategy ---
    strategy = ShortStraddleStrategy(config=ShortStraddleConfig())

    # --- Assemble and start the node ---
    node = TradingNode(config=node_config)

    # Factory names MUST match the keys in data_clients / exec_clients above
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)

    node.trader.add_strategy(strategy)
    node.build()  # connects clients, initialises instrument provider, starts WS

    try:
        node.run()  # blocks until Ctrl-C
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
