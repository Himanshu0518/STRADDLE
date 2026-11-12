"""
ShortStraddleStrategy — the NautilusTrader Strategy subclass for the BTC ATM Short Straddle.
"""
from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import IndexPriceUpdate
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events.order import OrderFilled
from nautilus_trader.model.events.position import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.trading.strategy import Strategy

from config import ShortStraddleConfig
from atm_selector import select_atm_straddle


class ShortStraddleStrategy(Strategy):
    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)
        self._orders_submitted: bool = False
        self._call_id: InstrumentId | None = None
        self._put_id: InstrumentId | None = None
        self._call_filled: bool = False
        self._put_filled: bool = False

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
        if event.instrument_id == self._call_id:
            self._call_filled = True
        elif event.instrument_id == self._put_id:
            self._put_filled = True
        if self._call_filled and self._put_filled:
            self._log.info(
                f"=== BOTH LEGS FILLED — Short straddle is LIVE!"
                f" CALL={self._call_id}  PUT={self._put_id} ==="
            )

    def on_position_opened(self, event: PositionOpened) -> None:
        self._log.info(
            f"POSITION OPENED: {event.instrument_id} side={event.side}"
            f" qty={event.quantity} avg_px={event.avg_px_open}"
        )
