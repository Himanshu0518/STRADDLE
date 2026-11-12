"""
TradingNode assembly: wires data/exec clients, strategy, and returns a ready-to-run node.
"""
from __future__ import annotations

from nautilus_trader.adapters.bybit import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)
from nautilus_trader.live.node import TradingNode

from config import build_node_config, ShortStraddleConfig
from strategy import ShortStraddleStrategy


def build_node(api_key: str, api_secret: str) -> TradingNode:
    """Assemble and build a TradingNode ready to run the short straddle strategy.

    Parameters
    ----------
    api_key : str
        Bybit API key.
    api_secret : str
        Bybit API secret.

    Returns
    -------
    TradingNode
        Fully built node (node.build() already called).
    """
    node_config = build_node_config(api_key, api_secret)
    strategy = ShortStraddleStrategy(config=ShortStraddleConfig())
    node = TradingNode(config=node_config)
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)
    node.trader.add_strategy(strategy)
    node.build()
    return node
