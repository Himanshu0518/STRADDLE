"""TradingNode assembly — wires Bybit clients, strategy, and returns a ready-to-run node."""
from __future__ import annotations

from nautilus_trader.adapters.bybit import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)
from nautilus_trader.live.node import TradingNode

from config import ShortStraddleConfig, build_node_config
from strategy import ShortStraddleStrategy


def build_node(api_key: str, api_secret: str) -> TradingNode:
    """Assemble, build, and return the TradingNode."""
    node = TradingNode(config=build_node_config(api_key, api_secret))
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)
    node.trader.add_strategy(ShortStraddleStrategy(config=ShortStraddleConfig()))
    node.build()
    return node
