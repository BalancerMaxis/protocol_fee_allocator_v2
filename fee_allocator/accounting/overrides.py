from abc import ABC, abstractmethod, ABCMeta
from decimal import Decimal
from typing import Dict, Type, TYPE_CHECKING
from fee_allocator.constants import POOL_OVERRIDES_URL
import requests

if TYPE_CHECKING:
    from fee_allocator.accounting.core_pools import PoolFee

overrides_data = requests.get(POOL_OVERRIDES_URL).json()


class PoolFeeOverrideMeta(ABCMeta):
    overrides: Dict[str, Type["PoolFeeOverride"]] = {}

    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        if name != "PoolFeeOverride":
            pool_id = attrs.get("pool_id")
            if pool_id:
                mcs.overrides[pool_id] = cls
        return cls


class PoolFeeOverride(ABC, metaclass=PoolFeeOverrideMeta):
    POOL_ID: str = None
    voting_pool: str = None
    market: str = None

    def __init__(self, core_pool: "PoolFee"):
        self.core_pool = core_pool

    @property
    @abstractmethod
    def to_aura_incentives_usd(self) -> Decimal:
        pass

    @property
    @abstractmethod
    def to_bal_incentives_usd(self) -> Decimal:
        pass


class RethWethOverride(PoolFeeOverride):
    pool_id = "0x1e19cf2d73a72ef1332c882f20534b6519be0276000200000000000000000112"
    voting_pool = overrides_data.get(pool_id).get("voting_pool_override")
    market = overrides_data.get(pool_id).get("market_override")

    @property
    def to_aura_incentives_usd(self) -> Decimal:
        return (
            self.core_pool.to_aura_incentives_usd
            if self.market == "aura"
            else Decimal(0)
        )

    @property
    def to_bal_incentives_usd(self) -> Decimal:
        return (
            self.core_pool.to_bal_incentives_usd if self.market == "bal" else Decimal(0)
        )


overrides = PoolFeeOverrideMeta.overrides
