"""ATM straddle instrument selection — pure logic, no NautilusTrader Strategy state."""
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
    """
    Return the (call, put) pair at the nearest-expiry ATM strike.

    Selection logic:
      1. Pull all CryptoOption instruments for the BYBIT venue from the cache.
      2. Discard expired contracts (expiration_ns <= now).
      3. Take the soonest expiry.
      4. Among that expiry's strikes, pick the one closest to index_price.
      5. Return the CALL and PUT at that strike.

    Returns None if the cache is empty, all options are expired, or either leg
    is missing at the ATM strike.
    """
    all_instruments = cache.instruments(venue=Venue("BYBIT"))
    options = [i for i in all_instruments if isinstance(i, CryptoOption)]
    if not options:
        log.error("No BTC options in cache — check InstrumentProviderConfig")
        return None

    log.info(f"Cache contains {len(options)} BTC option instruments")

    now_ns = clock.timestamp_ns()
    future_opts = [o for o in options if o.expiration_ns > now_ns]
    if not future_opts:
        log.error("All cached BTC options have expired")
        return None

    nearest_expiry_ns = min(o.expiration_ns for o in future_opts)
    expiry_opts = [o for o in future_opts if o.expiration_ns == nearest_expiry_ns]

    expiry_dt = datetime.datetime.fromtimestamp(
        nearest_expiry_ns / 1e9, tz=datetime.timezone.utc
    )
    log.info(f"Nearest expiry: {expiry_dt.date()} ({len(expiry_opts)} contracts)")

    strikes = sorted({float(o.strike_price) for o in expiry_opts})
    atm_strike = min(strikes, key=lambda s: abs(s - index_price))
    log.info(
        f"ATM strike: {atm_strike} "
        f"(index={index_price:.2f}, delta={abs(atm_strike - index_price):.2f})"
    )

    # Tolerance of 0.01 — BTC strikes are whole numbers so float comparison is exact;
    # this guards against any unexpected sub-dollar strike from the venue.
    atm_opts = [o for o in expiry_opts if abs(float(o.strike_price) - atm_strike) < 0.01]
    call = next((o for o in atm_opts if o.option_kind == OptionKind.CALL), None)
    put = next((o for o in atm_opts if o.option_kind == OptionKind.PUT), None)

    if call is None or put is None:
        log.error(
            f"Missing leg at ATM strike {atm_strike}: "
            f"{[o.id.symbol.value for o in atm_opts]}"
        )
        return None

    log.info(f"Selected CALL={call.id}  PUT={put.id}  expiry={expiry_dt.date()}")
    return call, put
