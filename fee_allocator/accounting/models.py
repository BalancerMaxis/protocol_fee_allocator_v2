from pydantic import BaseModel
from decimal import Decimal
from typing import Dict, NewType


RawPools = Dict[NewType("PoolId", str), NewType("Label", str)]
InputFees = Dict[NewType("ChainName", str), NewType("FeesCollected", int)]


class FeeConfig(BaseModel):
    min_aura_incentive: int
    min_existing_aura_incentive: int
    min_vote_incentive_amount: int
    vebal_share_pct: Decimal
    dao_share_pct: Decimal
    vote_incentive_pct: Decimal


class RawCorePoolData(BaseModel):
    mainnet: RawPools
    polygon: RawPools
    arbitrum: RawPools
    gnosis: RawPools
    zkevm: RawPools
    avalanche: RawPools
    base: RawPools

    def __getitem__(self, chain: str) -> RawPools:
        try:
            return getattr(self, chain)
        except AttributeError:
            raise KeyError(f"'{chain}' not found in raw core pools data")


class RerouteConfig(BaseModel):
    mainnet: Dict

    def model_post_init(self, __context):
        if any(self.__dict__.values()):
            raise ValueError(f"Reroute logic not implemented")
