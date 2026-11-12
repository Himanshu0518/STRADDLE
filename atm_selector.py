"""
Pure ATM straddle selection logic, independent of NautilusTrader Strategy state.
"""
from __future__ import annotations

import datetime

from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CryptoOption


def select_atm_straddle(
    cache,
    clock,
    log,
    index_price: float,
) -> tuple[CryptoOption, CryptoOption] | None:
    """Select the nearest-expiry ATM call and put from the instrument cache.

    Parameters
    ----------
    cache : Cache
        NautilusTrader cache holding loaded instruments.
    clock : Clock
        NautilusTrader clock used to determine current time.
    log : Logger
        NautilusTrader logger for diagnostic messages.
    index_price : float
        Current BTC index price used to determine the ATM strike.

    Returns
    -------
    tuple[CryptoOption, CryptoOption] or None
        (call, put) pair at the nearest-expiry ATM strike, or None on failure.
    """
    all_instruments = cache.instruments(venue=Venue("BYBIT"))
    options = [i for i in all_instruments if isinstance(i, CryptoOption)]
    if not options:
        log.error("No BTC options found in cache")
        return None
    log.info(f"Cache contains {len(options)} BTC option instruments")
    now_ns = clock.timestamp_ns()
    future_opts = [o for o in options if o.expiration_ns > now_ns]
    if not future_opts:
        log.error("All cached BTC options have already expired")
        return None
    nearest_expiry_ns = min(o.expiration_ns for o in future_opts)
    expiry_opts = [o for o in future_opts if o.expiration_ns == nearest_expiry_ns]
    expiry_dt = datetime.datetime.fromtimestamp(
        nearest_expiry_ns / 1e9, tz=datetime.timezone.utc
    )
    log.info(
        f"Nearest expiry: {expiry_dt.date()} ({len(expiry_opts)} contracts at this expiry)"
    )
    strikes = sorted({float(o.strike_price) for o in expiry_opts})
    if not strikes:
        log.error("No strikes found for nearest expiry")
        return None
    atm_strike = min(strikes, key=lambda s: abs(s - index_price))
    log.info(
        f"ATM strike selected: {atm_strike} (BTC index: {index_price:.2f},"
        f" delta: {abs(atm_strike - index_price):.2f})"
    )
    atm_opts = [o for o in expiry_opts if abs(float(o.strike_price) - atm_strike) < 0.01]
    call = next((o for o in atm_opts if o.option_kind == OptionKind.CALL), None)
    put = next((o for o in atm_opts if o.option_kind == OptionKind.PUT), None)
    if call is None or put is None:
        log.error(
            f"Could not find both legs at ATM strike {atm_strike}."
            f" Found: {[o.id.symbol.value for o in atm_opts]}"
        )
        return None
    log.info(
        f"ATM straddle selected — CALL: {call.id}  PUT: {put.id}  expiry: {expiry_dt.date()}"
    )
    return call, put
