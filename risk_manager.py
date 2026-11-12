"""
Risk management logic for the BTC ATM Short Straddle — Level 2.
This module has ZERO NautilusTrader dependencies — pure Python only.
"""
from __future__ import annotations


def check_exit_condition(
    entry_premium: float,
    current_ask_premium: float,
    stop_loss_pct: float = 0.01,
    take_profit_pct: float = 0.02,
) -> str | None:
    """
    Returns 'STOP_LOSS', 'TAKE_PROFIT', or None.

    Stop-loss:   current_ask_premium >= entry_premium * (1 + stop_loss_pct)
    Take-profit: current_ask_premium <= entry_premium * (1 - take_profit_pct)
    """
    if entry_premium <= 0:
        return None
    if current_ask_premium >= entry_premium * (1 + stop_loss_pct):
        return "STOP_LOSS"
    if current_ask_premium <= entry_premium * (1 - take_profit_pct):
        return "TAKE_PROFIT"
    return None
