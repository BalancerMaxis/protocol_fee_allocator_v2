from typing import Dict, Any
from .base import BribePlatform
from .stakedao import StakeDAOPlatform


def get_platform(book: Dict[str, str], run_config: Any) -> BribePlatform:
    """
    Get the StakeDAO platform instance.
    All bribes now go through StakeDAO VoteMarket v2.
    """
    return StakeDAOPlatform(book, run_config)
