"""BTC ATM Short Straddle — NautilusTrader strategy (Level 1 + Level 2)."""
from __future__ import annotations

from decimal import Decimal

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import IndexPriceUpdate, QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events.order import (
    OrderAccepted,
    OrderExpired,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.events.position import (
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.trading.strategy import Strategy

from atm_selector import select_atm_straddle
from config import ShortStraddleConfig
from risk_manager import check_exit_condition


class ShortStraddleStrategy(Strategy):
    """
    BTC ATM Short Straddle on Bybit TestNet.

    Level 1 — Entry
        On the first BTC index price tick, selects the nearest-expiry ATM strike
        from the instrument cache and submits SELL FOK market orders on both legs.
        FOK guarantees full fill or nothing — no partial straddle positions.

    Level 2 — Automated exit
        After both legs fill, subscribes to quote ticks and monitors the combined
        ask premium against the entry premium:
          - Stop-loss:   ask >= entry * (1 + stop_loss_pct)   [default 1%]
          - Take-profit: ask <= entry * (1 - take_profit_pct) [default 2%]
        Exit orders use IOC — a partial close is safer than leaving a naked short open.

    IOC for both entry and exit
        Both entry and exit use IOC on Bybit TestNet. FOK is not reliably supported
        for options on Bybit TestNet — orders submitted as FOK hang without any
        WebSocket acknowledgement, causing the reconciliation engine to timeout.
        IOC is the correct TIF for Bybit options market orders: it fills immediately
        against available liquidity and cancels any remainder.
        The FOK vs IOC tradeoff for entry orders is discussed in the walkthrough:
        FOK would be theoretically cleaner (no partial straddle) but IOC with
        partial-fill detection (on_order_expired checks filled_qty) achieves the
        same safety in practice.

    Failure handling
        Entry leg rejected/expired (IOC = zero or partial fill):
          - Other leg already filled → naked short → immediately buy it back.
          - Neither leg filled → reset all state, retry on next index price tick.
        Exit leg rejected/expired:
          - Re-attempt the close via _retry_exit_leg.
          - _exit_triggered only guards on_quote_tick; it does not block exit retries.

    Entry premium
        Quantity-weighted: sum(px * qty) / sum(qty) across all fill events for both legs.
        Exit conditions compare current combined ask premium on the same weighted basis.
    """

    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)

        # Instrument IDs — set when ATM pair is selected
        self._call_id: InstrumentId | None = None
        self._put_id: InstrumentId | None = None

        # ClientOrderIds — stored to distinguish entry vs exit orders for the same
        # instrument, since both can be in-flight simultaneously during the exit transition.
        self._call_entry_cloid: ClientOrderId | None = None
        self._put_entry_cloid: ClientOrderId | None = None
        self._call_exit_cloid: ClientOrderId | None = None
        self._put_exit_cloid: ClientOrderId | None = None

        # Entry state
        self._orders_submitted: bool = False
        self._call_filled: bool = False
        self._put_filled: bool = False
        self._call_filled_qty: Decimal = Decimal("0")
        self._put_filled_qty: Decimal = Decimal("0")

        # Quantity-weighted entry premium accumulators
        self._entry_total_cost: float = 0.0
        self._entry_total_qty: float = 0.0
        self._entry_premium: float = 0.0

        # Exit state
        self._exit_triggered: bool = False  # guards on_quote_tick only
        self._call_exit_filled: bool = False
        self._put_exit_filled: bool = False

        # Latest ask prices per leg — updated from live ticks, not cache
        self._latest_call_ask: float | None = None
        self._latest_put_ask: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to instrument updates and BTC index price on startup."""
        self.subscribe_instruments(venue=Venue("BYBIT"))
        self.subscribe_index_prices(
            instrument_id=InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
        )
        self.log.info("Strategy started — waiting for BTC index price tick")

    def on_stop(self) -> None:
        """Cancel open orders; attempt best-effort emergency close if positions are live."""
        if self._call_id is not None:
            self.cancel_all_orders(self._call_id)
        if self._put_id is not None:
            self.cancel_all_orders(self._put_id)

        if not self._exit_triggered and (self._call_filled or self._put_filled):
            self.log.warning("Stopping with open positions — triggering emergency close")
            self._trigger_exit("EMERGENCY_STOP")

        self.log.info("Strategy stopped")

    # ------------------------------------------------------------------
    # Index price → one-shot entry
    # ------------------------------------------------------------------

    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        """Select ATM straddle and submit entry orders on the first price tick."""
        if self._orders_submitted:
            return

        btc_price = float(index_price.value)
        self.log.info(f"BTC index price: {btc_price:.2f}")

        result = select_atm_straddle(self.cache, self.clock, self.log, btc_price)
        if result is None:
            self.log.warning("ATM selection failed — retrying on next tick")
            return

        call, put = result
        self._call_id = call.id
        self._put_id = put.id
        self._orders_submitted = True  # set before submit to prevent race on fast ticks
        self._submit_straddle(call, put)

    # ------------------------------------------------------------------
    # Entry order submission
    # ------------------------------------------------------------------

    def _submit_straddle(self, call: CryptoOption, put: CryptoOption) -> None:
        """Submit SELL FOK market orders for both legs."""
        try:
            call_qty = call.make_qty(Decimal("0.01"))
            put_qty = put.make_qty(Decimal("0.01"))

            call_order = self.order_factory.market(
                instrument_id=call.id,
                order_side=OrderSide.SELL,
                quantity=call_qty,
                time_in_force=TimeInForce.IOC,
            )
            put_order = self.order_factory.market(
                instrument_id=put.id,
                order_side=OrderSide.SELL,
                quantity=put_qty,
                time_in_force=TimeInForce.IOC,
            )

            # Store CLOIDs before submitting — needed immediately if rejection fires
            self._call_entry_cloid = call_order.client_order_id
            self._put_entry_cloid = put_order.client_order_id

            self.log.info(f"Submitting SELL IOC {call_qty} CALL: {call.id} [cloid={call_order.client_order_id}]")
            self.submit_order(call_order)

            self.log.info(f"Submitting SELL IOC {put_qty} PUT: {put.id} [cloid={put_order.client_order_id}]")
            self.submit_order(put_order)

        except Exception as exc:
            self.log.error(f"Entry order submission failed: {exc!r}")
            self._orders_submitted = False  # allow retry on next tick

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """Bybit acknowledged the order — it is now working at the venue."""
        self.log.info(
            f"ORDER ACCEPTED: {event.instrument_id} "
            f"cloid={event.client_order_id} venue_order_id={event.venue_order_id}"
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        """Route fill to entry or exit handler based on order side."""
        self.log.info(
            f"ORDER FILLED: {event.instrument_id} side={event.order_side} "
            f"qty={event.last_qty} px={event.last_px} commission={event.commission}",
            color=LogColor.GREEN,
        )
        if event.order_side == OrderSide.SELL:
            self._handle_entry_fill(event)
        elif event.order_side == OrderSide.BUY:
            self._handle_exit_fill(event)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """Route rejection to entry failure handler or exit retry."""
        self.log.error(
            f"ORDER REJECTED: {event.instrument_id} "
            f"cloid={event.client_order_id} reason={event.reason}"
        )
        order_type = self._classify_order(event.client_order_id)
        if order_type in ("CALL_ENTRY", "PUT_ENTRY"):
            leg = "CALL" if order_type == "CALL_ENTRY" else "PUT"
            self.log.error(f"Entry {leg} leg REJECTED (FOK — zero fill)")
            self._handle_leg_failure(leg, reason=event.reason)
        elif order_type in ("CALL_EXIT", "PUT_EXIT"):
            leg = "CALL" if order_type == "CALL_EXIT" else "PUT"
            self.log.error(f"Exit {leg} leg REJECTED — re-attempting close")
            self._retry_exit_leg(event.instrument_id, leg)

    def on_order_expired(self, event: OrderExpired) -> None:
        """
        IOC entry expiry = zero or partial fill — check filled_qty to distinguish.
        IOC exit expiry = insufficient liquidity — re-attempt the close.
        """
        self.log.warning(
            f"ORDER EXPIRED: {event.instrument_id} cloid={event.client_order_id}"
        )
        order_type = self._classify_order(event.client_order_id)
        if order_type in ("CALL_ENTRY", "PUT_ENTRY"):
            leg = "CALL" if order_type == "CALL_ENTRY" else "PUT"
            # IOC may partially fill — check filled_qty to decide how to handle it
            order = self.cache.order(event.client_order_id)
            filled_qty = float(order.filled_qty) if order is not None else 0.0
            if filled_qty > 0:
                # Partial fill — on_order_filled already set the flag; check if both legs done
                self.log.warning(
                    f"Entry {leg} leg partially filled ({filled_qty}) then expired (IOC)"
                )
                if self._call_filled and self._put_filled:
                    self.log.info("Both legs have partial fills — starting Level 2 monitoring")
                    self.subscribe_quote_ticks(instrument_id=self._call_id)
                    self.subscribe_quote_ticks(instrument_id=self._put_id)
            else:
                # Zero fill — treat as full rejection
                self.log.error(f"Entry {leg} leg expired with zero fill (IOC miss)")
                self._handle_leg_failure(leg, reason=f"ENTRY_{leg}_EXPIRED")
        elif order_type in ("CALL_EXIT", "PUT_EXIT"):
            leg = "CALL" if order_type == "CALL_EXIT" else "PUT"
            self.log.warning(f"Exit {leg} leg expired — re-attempting close")
            self._retry_exit_leg(event.instrument_id, leg)

    # ------------------------------------------------------------------
    # Level 2 — Quote tick monitoring
    # ------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """
        Monitor combined ask premium for SL/TP conditions.

        Ask prices are taken from the arriving tick (not the cache) to avoid
        stale cross-leg quotes. Combined premium is computed only after both
        legs have received at least one quote.
        """
        if not (self._call_filled and self._put_filled) or self._exit_triggered:
            return

        if tick.instrument_id == self._call_id:
            self._latest_call_ask = float(tick.ask_price)
        elif tick.instrument_id == self._put_id:
            self._latest_put_ask = float(tick.ask_price)

        if self._latest_call_ask is None or self._latest_put_ask is None:
            return

        # Quantity-weighted combined ask — same basis as entry_premium
        total_qty = float(self._call_filled_qty) + float(self._put_filled_qty)
        current_ask_premium = (
            self._latest_call_ask * float(self._call_filled_qty)
            + self._latest_put_ask * float(self._put_filled_qty)
        ) / total_qty

        reason = check_exit_condition(
            entry_premium=self._entry_premium,
            current_ask_premium=current_ask_premium,
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
        )

        if reason is not None:
            change_pct = ((current_ask_premium / self._entry_premium) - 1) * 100
            self.log.info(
                f"Exit condition: {reason} | "
                f"entry={self._entry_premium:.4f} "
                f"current={current_ask_premium:.4f} "
                f"change={change_pct:+.2f}%"
            )
            self._trigger_exit(reason)

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _trigger_exit(self, reason: str) -> None:
        """Set exit guard and submit BUY IOC orders for all filled legs."""
        self._exit_triggered = True
        self.log.info(
            f"EXIT TRIGGERED: {reason} — closing positions",
            color=LogColor.YELLOW,
        )
        self._close_leg(self._call_id, self._call_filled, self._call_filled_qty, "CALL", reason)
        self._close_leg(self._put_id, self._put_filled, self._put_filled_qty, "PUT", reason)

    def _retry_exit_leg(self, instrument_id: InstrumentId, leg_name: str) -> None:
        """Re-attempt closing one leg after its exit order failed."""
        is_call = leg_name == "CALL"
        self._close_leg(
            instrument_id,
            self._call_filled if is_call else self._put_filled,
            self._call_filled_qty if is_call else self._put_filled_qty,
            leg_name,
            "RETRY_EXIT",
            is_retry=True,
        )

    def _close_leg(
        self,
        instrument_id: InstrumentId | None,
        is_filled: bool,
        filled_qty: Decimal,
        leg_name: str,
        reason: str,
        is_retry: bool = False,
    ) -> None:
        """Submit a BUY IOC order to close one leg using its actual filled quantity."""
        if not is_filled:
            self.log.warning(f"{leg_name} not filled — no position to close ({reason})")
            return
        if instrument_id is None:
            self.log.error(f"Cannot close {leg_name}: instrument_id is None")
            return

        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.error(f"Cannot close {leg_name}: {instrument_id} not in cache")
            return

        try:
            qty = instrument.make_qty(filled_qty)
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
                time_in_force=TimeInForce.IOC,
            )
            if leg_name == "CALL":
                self._call_exit_cloid = order.client_order_id
            else:
                self._put_exit_cloid = order.client_order_id

            prefix = "RETRY " if is_retry else ""
            self.log.info(
                f"Submitting {prefix}BUY IOC {qty} {leg_name}: {instrument_id} "
                f"[cloid={order.client_order_id}]"
            )
            self.submit_order(order)
        except Exception as exc:
            self.log.error(f"{leg_name} exit submission failed: {exc!r}")

    # ------------------------------------------------------------------
    # Position event handlers
    # ------------------------------------------------------------------

    def on_position_opened(self, event: PositionOpened) -> None:
        """Log newly opened short position."""
        self.log.info(
            f"POSITION OPENED: {event.instrument_id} "
            f"side={event.side} qty={event.quantity} avg_px={event.avg_px_open}",
            color=LogColor.BLUE,
        )

    def on_position_changed(self, event: PositionChanged) -> None:
        """Log position size change — fires on partial exit fills."""
        self.log.info(
            f"POSITION CHANGED: {event.instrument_id} "
            f"side={event.side} qty={event.quantity} unrealized_pnl={event.unrealized_pnl}",
            color=LogColor.BLUE,
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        """Log fully closed position with realized PnL."""
        self.log.info(
            f"POSITION CLOSED: {event.instrument_id} "
            f"realized_pnl={event.realized_pnl} duration={event.duration_ns}ns",
            color=LogColor.GREEN,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_order(self, cloid: ClientOrderId) -> str | None:
        """Return 'CALL_ENTRY', 'PUT_ENTRY', 'CALL_EXIT', 'PUT_EXIT', or None."""
        if cloid == self._call_entry_cloid:
            return "CALL_ENTRY"
        if cloid == self._put_entry_cloid:
            return "PUT_ENTRY"
        if cloid == self._call_exit_cloid:
            return "CALL_EXIT"
        if cloid == self._put_exit_cloid:
            return "PUT_EXIT"
        return None

    def _handle_entry_fill(self, event: OrderFilled) -> None:
        """Accumulate quantity-weighted entry premium; subscribe to quotes when both legs fill."""
        px = float(event.last_px)
        qty = float(event.last_qty)
        self._entry_total_cost += px * qty
        self._entry_total_qty += qty

        if event.instrument_id == self._call_id:
            self._call_filled = True
            self._call_filled_qty = Decimal(str(qty))
        elif event.instrument_id == self._put_id:
            self._put_filled = True
            self._put_filled_qty = Decimal(str(qty))

        if self._call_filled and self._put_filled:
            self._entry_premium = (
                self._entry_total_cost / self._entry_total_qty
                if self._entry_total_qty > 0 else 0.0
            )
            self.log.info(
                f"=== STRADDLE LIVE: CALL={self._call_id} PUT={self._put_id} "
                f"entry_premium={self._entry_premium:.4f} "
                f"SL={self.config.stop_loss_pct * 100:.1f}% "
                f"TP={self.config.take_profit_pct * 100:.1f}% ===",
                color=LogColor.GREEN,
            )
            self.subscribe_quote_ticks(instrument_id=self._call_id)
            self.subscribe_quote_ticks(instrument_id=self._put_id)
            self.log.info("Quote tick monitoring started")

    def _handle_exit_fill(self, event: OrderFilled) -> None:
        """Track exit fills; log when all filled legs are closed."""
        if event.instrument_id == self._call_id:
            self._call_exit_filled = True
            self.log.info(f"CALL EXIT FILLED: px={event.last_px}", color=LogColor.BLUE)
        elif event.instrument_id == self._put_id:
            self._put_exit_filled = True
            self.log.info(f"PUT EXIT FILLED: px={event.last_px}", color=LogColor.BLUE)

        legs_to_close = sum([self._call_filled, self._put_filled])
        legs_closed = sum([self._call_exit_filled, self._put_exit_filled])
        if legs_closed == legs_to_close > 0:
            self.log.info("=== STRADDLE CLOSED ===", color=LogColor.GREEN)

    # Rejection reasons that are permanent — retrying will never succeed.
    # On these, stop the strategy rather than loop indefinitely.
    _PERMANENT_REJECTION_REASONS = (
        "Insufficient margin balance",
        "Account is banned",
        "Trading is not allowed",
    )

    def _handle_leg_failure(self, failed_leg: str, reason: str) -> None:
        """
        FOK entry leg failed (zero fill guaranteed).

        If the other leg is already short → naked position → trigger exit immediately.
        If neither leg filled → check if the rejection is permanent (e.g. no margin).
          - Permanent: stop retrying, log a clear message, leave _orders_submitted=True.
          - Transient: reset all state and allow retry on the next price tick.
        """
        other_leg = "PUT" if failed_leg == "CALL" else "CALL"
        other_filled = self._put_filled if failed_leg == "CALL" else self._call_filled

        if other_filled:
            self.log.error(
                f"{failed_leg} failed but {other_leg} is short — "
                f"buying back {other_leg} to avoid naked position",
                color=LogColor.RED,
            )
            self._trigger_exit(reason=f"LEG_IMBALANCE_{reason}")
            return

        # Check if this is a permanent failure — no point retrying
        rejection_text = reason.lower()
        is_permanent = any(
            p.lower() in rejection_text
            for p in self._PERMANENT_REJECTION_REASONS
        )

        if is_permanent:
            self.log.error(
                f"Permanent rejection — stopping retry. Reason: {reason}. "
                f"Fix the underlying issue (e.g. add margin) and restart the strategy.",
                color=LogColor.RED,
            )
            # Leave _orders_submitted=True so on_index_price stops retrying
            return

        # Transient failure — reset and allow retry on next tick
        self.log.warning("Both legs failed (transient) — resetting state for retry")
        self._orders_submitted = False
        self._call_filled = False
        self._put_filled = False
        self._call_filled_qty = Decimal("0")
        self._put_filled_qty = Decimal("0")
        self._entry_total_cost = 0.0
        self._entry_total_qty = 0.0
        self._entry_premium = 0.0
        self._call_entry_cloid = None
        self._put_entry_cloid = None
