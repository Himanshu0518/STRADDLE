"""
Entry point for the BTC ATM Short Straddle strategy.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

from node import build_node


def main() -> None:
    """Load credentials and run the trading node."""
    load_dotenv()
    api_key = os.environ["API_KEY"]
    api_secret = os.environ["API_SECRET"]
    node = build_node(api_key, api_secret)
    try:
        node.run()
    finally:
        node.stop()
        node.dispose()


if __name__ == "__main__":
    main()
