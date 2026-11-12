"""
ShortStraddleStrategy — the NautilusTrader Strategy subclass for the BTC ATM Short Straddle.
Level 2: automated stop-loss and take-profit exit via quote tick monitoring.
"""
from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import IndexPriceUpdate, QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events.order import OrderFilled
from nautilus_trader.model.events.position import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.trading.strategy import Strategy

from config import ShortStraddleConfig
from atm_selector import select_atm_straddle
from risk_manager import check_exit_condition


class ShortStraddleStrategy(Strategy):
    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)
        # Level 1 state
        self._orders_submitted: bool = False
        self._call_id: InstrumentId | None = None
        self._put_id: InstrumentId | None = None
        self._call_filled: bool = False
        self._put_filled: bool = False
        # Level 2 state
        self._entry_premium: float = 0.0
        self._exit_triggered: bool = False
        self._call_exit_filled: bool = False
        self._put_exit_filled: bool = False

    def on_start(self) -> None:
        self.subscribe_instruments(venue=Venue("BYBIT"))
        btc_linear_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
        self.subscribe_index_prices(instrument_id=btc_linear_id)
        self._log.info("Strategy started — waiting for BTC index price tick")

    def on_stop(self) -> None:
        if self._call_id is not None:
            self.cancel_all_orders(self._call_id)
        if self._put_id is not None:
            self.cancel_all_orders(self._put_id)
        # If exit was never triggered and positions may be open, attempt to close
        if not self._exit_triggered and (self._call_filled or self._put_filled):
            self._log.warning(
                "Stopping with open positions — triggering emergency exit"
            )
            self._trigger_exit("EMERGENCY_STOP")
        self._log.info("Strategy stopped — open orders cancelled (if any)")

    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        if self._orders_submitted:
            return
        btc_price = float(index_price.value)
        self._log.info(f"BTC index price: {btc_price:.2f}")
        result = select_atm_straddle(self.cache, self.clock, self._log, btc_price)
        if result is None:
            self._log.warning("ATM straddle selection failed; will retry on next index price tick")
            return
        call, put = result
        self._call_id = call.id
        self._put_id = put.id
        self._orders_submitted = True
        self._submit_straddle(call, put)

    def _submit_straddle(self, call: CryptoOption, put: CryptoOption) -> None:
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
            self._log.info(f"Submitting SELL {call_qty} CALL: {call.id}")
            self.submit_order(call_order)
            self._log.info(f"Submitting SELL {put_qty} PUT: {put.id}")
            self.submit_order(put_order)
        except Exception as exc:
            self._log.error(f"Order submission failed: {exc!r}")

    def on_order_filled(self, event: OrderFilled) -> None:
        self._log.info(
            f"ORDER FILLED: {event.instrument_id} side={event.order_side}"
            f" qty={event.last_qty} px={event.last_px} commission={event.commission}"
        )
        if event.order_side == OrderSide.SELL:
            # Entry fill — accumulate premium
            self._entry_premium += float(event.last_px)
            if event.instrument_id == self._call_id:
                self._call_filled = True
            elif event.instrument_id == self._put_id:
                self._put_filled = True
            if self._call_filled and self._put_filled:
                self._log.info(
                    f"=== BOTH LEGS FILLED — Short straddle is LIVE!"
                    f" CALL={self._call_id}  PUT={self._put_id}"
                    f" entry_premium={self._entry_premium:.4f} ==="
                )
                # Subscribe to quote ticks for both legs to enable exit monitoring
                self.subscribe_quote_ticks(instrument_id=self._call_id)
                self.subscribe_quote_ticks(instrument_id=self._put_id)
                self._log.info(
                    f"Subscribed to quote ticks for {self._call_id} and {self._put_id}"
                    f" — SL={self.config.stop_loss_pct*100:.1f}%"
                    f"  TP={self.config.take_profit_pct*100:.1f}%"
                )
        elif event.order_side == OrderSide.BUY:
            # Exit fill
            if event.instrument_id == self._call_id:
                self._call_exit_filled = True
                self._log.info(f"CALL EXIT FILLED: {event.instrument_id} px={event.last_px}")
            elif event.instrument_id == self._put_id:
                self._put_exit_filled = True
                self._log.info(f"PUT EXIT FILLED: {event.instrument_id} px={event.last_px}")
            if self._call_exit_filled and self._put_exit_filled:
                self._log.info("=== STRADDLE CLOSED ===")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Monitor combined ask premium for stop-loss / take-profit exit conditions."""
        # Only active after both legs are filled and exit not yet triggered
        if not (self._call_filled and self._put_filled):
            return
        if self._exit_triggered:
            return
        # Retrieve latest quotes from cache
        call_quote = self.cache.quote_tick(self._call_id)
        put_quote = self.cache.quote_tick(self._put_id)
        if call_quote is None or put_quote is None:
            return  # Wait until both legs have quotes
        current_ask_premium = float(call_quote.ask_price) + float(put_quote.ask_price)
        reason = check_exit_condition(
            entry_premium=self._entry_premium,
            current_ask_premium=current_ask_premium,
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
        )
        if reason is not None:
            self._trigger_exit(reason)

    def _trigger_exit(self, reason: str) -> None:
        """Submit BUY IOC orders on both legs to close the straddle."""
        self._exit_triggered = True  # Set guard first to prevent duplicate exits
        self._log.info(f"EXIT TRIGGERED: {reason} — closing straddle positions")
        call_instrument = self.cache.instrument(self._call_id)
        put_instrument = self.cache.instrument(self._put_id)
        if self._call_filled:
            if call_instrument is not None:
                try:
                    qty = call_instrument.make_qty(Decimal("0.01"))
                    order = self.order_factory.market(
                        instrument_id=self._call_id,
                        order_side=OrderSide.BUY,
                        quantity=qty,
                        time_in_force=TimeInForce.IOC,
                    )
                    self._log.info(f"Submitting BUY (exit) {qty} CALL: {self._call_id}")
                    self.submit_order(order)
                except Exception as exc:
                    self._log.error(f"CALL exit order failed: {exc!r}")
            else:
                self._log.error(f"Cannot close CALL: instrument {self._call_id} not in cache")
        else:
            self._log.warning(f"CALL leg not filled — skipping CALL exit order (reason: {reason})")
        if self._put_filled:
            if put_instrument is not None:
                try:
                    qty = put_instrument.make_qty(Decimal("0.01"))
                    order = self.order_factory.market(
                        instrument_id=self._put_id,
                        order_side=OrderSide.BUY,
                        quantity=qty,
                        time_in_force=TimeInForce.IOC,
                    )
                    self._log.info(f"Submitting BUY (exit) {qty} PUT: {self._put_id}")
                    self.submit_order(order)
                except Exception as exc:
                    self._log.error(f"PUT exit order failed: {exc!r}")
            else:
                self._log.error(f"Cannot close PUT: instrument {self._put_id} not in cache")
        else:
            self._log.warning(f"PUT leg not filled — skipping PUT exit order (reason: {reason})")

    def on_position_opened(self, event: PositionOpened) -> None:
        self._log.info(
            f"POSITION OPENED: {event.instrument_id} side={event.side}"
            f" qty={event.quantity} avg_px={event.avg_px_open}"
        )
