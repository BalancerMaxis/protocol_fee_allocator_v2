from pydantic import BaseModel
from decimal import Decimal
from typing import Dict, NewType

PoolId = NewType("PoolId", str)
Label = NewType("Label", str)


class FeeConfig(BaseModel):
    min_aura_incentive: int
    min_existing_aura_incentive: int
    min_vote_incentive_amount: int
    vebal_share_pct: Decimal
    dao_share_pct: Decimal
    vote_incentive_pct: Decimal


class RawCorePoolData(BaseModel):
    mainnet: Dict[PoolId, Label]
    polygon: Dict[PoolId, Label]
    arbitrum: Dict[PoolId, Label]
    gnosis: Dict[PoolId, Label]
    zkevm: Dict[PoolId, Label]
    avalanche: Dict[PoolId, Label]
    base: Dict[PoolId, Label]

    def __getitem__(self, chain: str) -> Dict[PoolId, Label]:
        try:
            return getattr(self, chain)
        except AttributeError:
            raise KeyError(f"'{chain}' not found in raw core pools data")
