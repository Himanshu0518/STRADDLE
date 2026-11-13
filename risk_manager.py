"""Stop-loss and take-profit exit conditions — pure Python, no framework dependencies."""
from __future__ import annotations


def check_exit_condition(
    entry_premium: float,
    current_ask_premium: float,
    stop_loss_pct: float = 0.01,
    take_profit_pct: float = 0.02,
) -> str | None:
    """
    Evaluate SL/TP conditions for a short straddle.

    Parameters
    ----------
    entry_premium : float
        Quantity-weighted average price paid to enter the straddle.
    current_ask_premium : float
        Current combined ask price to buy back both legs.
    stop_loss_pct : float
        Close if buyback cost rises above entry by this fraction (default 1%).
    take_profit_pct : float
        Close if buyback cost falls below entry by this fraction (default 2%).

    Returns
    -------
    'STOP_LOSS', 'TAKE_PROFIT', or None.
    """
    if entry_premium <= 0:
        return None
    if current_ask_premium >= entry_premium * (1 + stop_loss_pct):
        return "STOP_LOSS"
    if current_ask_premium <= entry_premium * (1 - take_profit_pct):
        return "TAKE_PROFIT"
    return None
