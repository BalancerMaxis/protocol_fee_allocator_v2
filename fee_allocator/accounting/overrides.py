from abc import ABC, abstractmethod, ABCMeta
from decimal import Decimal
from typing import Dict, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from fee_allocator.accounting.core_pools import CorePool


class CorePoolOverrideMeta(ABCMeta):
    overrides: Dict[str, Type["CorePoolOverride"]] = {}

    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        if name != "CorePoolOverride":
            pool_id = attrs.get("pool_id")
            if pool_id:
                mcs.overrides[pool_id] = cls
        return cls


class CorePoolOverride(ABC, metaclass=CorePoolOverrideMeta):
    POOL_ID: str = None

    def __init__(self, core_pool: "CorePool"):
        self.core_pool = core_pool

    @property
    @abstractmethod
    def to_aura_incentives_usd(self) -> Decimal:
        pass

    @property
    @abstractmethod
    def to_bal_incentives_usd(self) -> Decimal:
        pass


class RethWethOverride(CorePoolOverride):
    pool_id = "0x1e19cf2d73a72ef1332c882f20534b6519be0276000200000000000000000112"

    @property
    def to_aura_incentives_usd(self) -> Decimal:
        return Decimal(0)

    @property
    def to_bal_incentives_usd(self) -> Decimal:
        return self.core_pool.redistributed.total_to_incentives_usd


overrides = CorePoolOverrideMeta.overrides
