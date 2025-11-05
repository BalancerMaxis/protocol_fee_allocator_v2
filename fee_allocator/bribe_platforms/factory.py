from typing import Dict, Any, List
from .base import BribePlatform
from .hiddenhand import HiddenHandPlatform
from .paladin import PaladinPlatform
from .stakedao import StakeDAOPlatform


class BribePlatformFactory:
    """Factory for creating bribe platform instances based on configuration"""

    _platforms = {
        "hh": HiddenHandPlatform,
        "paladin": PaladinPlatform,
        "stakedao": StakeDAOPlatform,
    }

    @classmethod
    def register_platform(cls, config_key: str, platform_class: type):
        """
        Register a new platform class

        Args:
            config_key: The configuration key (e.g., 'hh', 'paladin', 'stakedao')
            platform_class: The platform class that implements BribePlatform
        """
        cls._platforms[config_key] = platform_class

    @classmethod
    def get_platform(cls, platform_name: str, book: Dict[str, str], run_config: Any) -> BribePlatform:
        """
        Get platform instance based on platform name

        Args:
            platform_name: Platform name ('hh', 'paladin', 'stakedao')
            book: Address book dictionary
            run_config: Run configuration object

        Returns:
            BribePlatform instance

        Raises:
            ValueError: If platform_name is not recognized
        """
        if not platform_name or platform_name == "hh":
            return HiddenHandPlatform(book, run_config)

        platform_class = cls._platforms.get(platform_name)
        if not platform_class:
            raise ValueError(f"Unknown platform: {platform_name}. Available: {list(cls._platforms.keys())}")

        return platform_class(book, run_config)

    @classmethod
    def get_supported_markets(cls, platform_name: str, book: Dict[str, str], run_config: Any) -> List[str]:
        """
        Get list of supported markets for a platform

        Args:
            platform_name: Platform name
            book: Address book dictionary
            run_config: Run configuration object

        Returns:
            List of supported market names
        """
        platform = cls.get_platform(platform_name, book, run_config)
        return platform.supported_markets

