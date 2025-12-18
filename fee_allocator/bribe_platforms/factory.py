from typing import Dict, Any
from .base import BribePlatform
from .hiddenhand import HiddenHandPlatform
from .paladin import PaladinPlatform
from .stakedao import StakeDAOPlatform


def get_platform(platform_name: str, book: Dict[str, str], run_config: Any) -> BribePlatform:
    """
    Get platform instance based on platform name.

    Args:
        platform_name: 'stakedao', 'paladin', or 'hh'
        book: Address book dictionary
        run_config: Run configuration object

    Returns:
        BribePlatform instance
    """
    if platform_name == "stakedao":
        return StakeDAOPlatform(book, run_config)
    elif platform_name == "paladin":
        return PaladinPlatform(book, run_config)
    elif platform_name == "hh":
        return HiddenHandPlatform(book, run_config)
    raise ValueError(f"Unknown platform: {platform_name}")
