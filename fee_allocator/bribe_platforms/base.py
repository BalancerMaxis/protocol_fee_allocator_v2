from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, Any, List
import pandas as pd


class BribePlatform(ABC):
    """Abstract base class for bribe platform implementations"""

    def __init__(self, book: Dict[str, str], run_config: Any):
        self.book = book
        self.run_config = run_config

    @abstractmethod
    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        """
        Process bribes for this platform

        Args:
            bribes_df: DataFrame with columns [target, platform, amount, bribe_platform]
            builder: SafeTxBuilder instance for building transactions
            usdc: SafeContract instance for USDC token
        """
        pass

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """
        Calculate total USDC amount that needs approval for this platform

        Args:
            bribes_df: DataFrame with bribe information

        Returns:
            Total amount in USDC wei that needs approval
        """
        return 0

    @abstractmethod
    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """
        Validate if gauge is properly configured for this platform

        Args:
            gauge_address: Address of the gauge to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return platform identifier for CSV/reporting"""
        pass

    @property
    @abstractmethod
    def supported_markets(self) -> List[str]:
        """Return list of supported markets (e.g., ['aura', 'balancer'])"""
        pass

    @abstractmethod
    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        """
        Get the platform name to use for a specific market.
        This handles platform-specific routing logic.

        Args:
            market: The market ('aura' or 'balancer')
            voting_pool_override: The voting pool override setting

        Returns:
            Platform name to use for this market (e.g., 'hh', 'paladin', 'stakedao')
        """
        pass