from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, Any
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
            bribes_df: DataFrame with columns [target, amount, is_alliance]
            builder: SafeTxBuilder instance for building transactions
            usdc: SafeContract instance for USDC token
        """
        pass

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
        """Return platform identifier for reporting"""
        pass
