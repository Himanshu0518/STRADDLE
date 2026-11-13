"""Strategy config and TradingNode config builder."""
from __future__ import annotations

import os

from nautilus_trader.adapters.bybit import BybitDataClientConfig, BybitExecClientConfig
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    StrategyConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.nautilus_pyo3 import BybitEnvironment, BybitProductType


class ShortStraddleConfig(StrategyConfig, frozen=True):
    """Configurable risk parameters for the short straddle strategy."""
    stop_loss_pct: float = 0.01    # 1%  — close if combined ask rises by this amount
    take_profit_pct: float = 0.02  # 2%  — close if combined ask falls by this amount


def build_node_config(api_key: str, api_secret: str) -> TradingNodeConfig:
    """Build TradingNodeConfig for Bybit TestNet with BTC options only."""
    instrument_cfg = InstrumentProviderConfig(
        load_all=True,
        filters={"base_coin": "BTC"},
    )
    data_client_cfg = BybitDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        product_types=(BybitProductType.OPTION,),
        environment=BybitEnvironment.TESTNET,
        instrument_provider=instrument_cfg,
    )
    exec_client_cfg = BybitExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        product_types=(BybitProductType.OPTION,),
        environment=BybitEnvironment.TESTNET,
        instrument_provider=instrument_cfg,
    )
    return TradingNodeConfig(
        trader_id="TRADER-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={"BYBIT": data_client_cfg},
        exec_clients={"BYBIT": exec_client_cfg},
        exec_engine=LiveExecEngineConfig(reconciliation=True),
    )
