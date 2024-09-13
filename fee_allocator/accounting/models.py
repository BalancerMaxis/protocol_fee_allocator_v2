from pydantic import BaseModel
from decimal import Decimal
from typing import Dict, NewType


Pools = Dict[NewType("PoolId", str), NewType("Symbol", str)]
InputFees = Dict[NewType("CorePoolChainName", str), NewType("FeesCollected", int)]


class GlobalFeeConfig(BaseModel):
    min_aura_incentive: int
    min_existing_aura_incentive: int
    min_vote_incentive_amount: int
    vebal_share_pct: Decimal
    dao_share_pct: Decimal
    vote_incentive_pct: Decimal


class RerouteConfig(BaseModel):
    mainnet: Dict

    def model_post_init(self, __context):
        if any(self.__dict__.values()):
            raise ValueError(f"Reroute logic not implemented")

