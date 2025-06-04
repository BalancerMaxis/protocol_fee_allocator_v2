from pydantic import BaseModel
from decimal import Decimal
from typing import Dict, NewType


Pools = Dict[NewType("PoolId", str), NewType("Symbol", str)]
InputFees = Dict[NewType("CorePoolChainName", str), NewType("FeesCollected", int)]


class GlobalFeeConfig(BaseModel):
    """
    Represents the global fee configuration for the fee allocation process.
    Models the data sourced from the FEE_CONSTANTS_URL endpoint.
    """

    min_aura_incentive: int
    min_existing_aura_incentive: int
    min_vote_incentive_amount: int
    
    # Core pool fee splits
    vebal_share_pct: Decimal
    dao_share_pct: Decimal
    vote_incentive_pct: Decimal
    
    # Non-core pool fee splits
    noncore_vebal_share_pct: Decimal
    noncore_dao_share_pct: Decimal
    
    # Beets fee split (https://forum.balancer.fi/t/bip-800-deploy-balancer-v3-on-op-mainnet)
    beets_share_pct: Decimal


class AlliancePool(BaseModel):
    """
    Represents a pool that is part of the Balancer Alliance program.
    """
    pool_id: str
    network: str
    partner: str
    pool_type: str
    eligibility_date: str
    active: bool


class AllianceMember(BaseModel):
    """
    Represents a member of the Balancer Alliance program.
    """
    name: str
    multisig_address: str
    active: bool
    join_date: str
    last_lock_date: str
    pools: list[AlliancePool]


class AllianceFeeAllocation(BaseModel):
    """
    Represents the fee allocation configuration for Alliance pools.
    """
    vebal_share_pct: Decimal
    vote_incentive_pct: Decimal | None = None  # None for non-core pools
    partner_share_pct: Decimal
    dao_share_pct: Decimal


class AllianceThresholds(BaseModel):
    """
    Represents threshold values for Alliance pool eligibility.
    """
    v3_min_tvl: Decimal
    v2_min_tvl: Decimal


class PartnerPool(BaseModel):
    """
    Represents a partner pool with custom fee allocation.
    """
    pool_id: str
    network: str
    active: bool


class PartnerFeeAllocation(BaseModel):
    """
    Represents the fee allocation configuration for partner pools.
    """
    vebal_share_pct: Decimal
    vote_incentive_pct: Decimal
    partner_share_pct: Decimal
    dao_share_pct: Decimal


class Partner(BaseModel):
    """
    Represents a partner with custom fee allocations.
    """
    name: str
    active: bool
    fee_allocations: dict[str, PartnerFeeAllocation]  # "core" and "non_core"
    pools: list[PartnerPool]


class AllianceConfig(BaseModel):
    """
    Represents the complete Alliance configuration including members and fee allocations.
    Models the data sourced from the ALLIANCE_CONSTANTS_URL endpoint.
    """
    alliance_members: list[AllianceMember]
    alliance_fee_allocations: dict[str, AllianceFeeAllocation]
    alliance_thresholds: AllianceThresholds
    partners: list[Partner] | None = None

    def get_pool_fee_config(self, pool_id: str, network: str, is_core: bool) -> AllianceFeeAllocation | None:
        """
        Returns the fee allocation configuration for a specific pool if it's part of the Alliance program.
        Returns None if the pool is not part of the Alliance program.
        """
        for member in self.alliance_members:
            for pool in member.pools:
                if pool.pool_id == pool_id and pool.network == network and pool.active:
                    return self.alliance_fee_allocations["core" if is_core else "non_core"]
        return None
    
    def get_partner_pool_config(self, pool_id: str, network: str, is_core: bool) -> tuple[str, PartnerFeeAllocation] | None:
        """
        Returns the partner name and fee allocation configuration for a specific partner pool.
        Returns None if the pool is not a partner pool.
        """
        if not self.partners:
            return None
            
        for partner in self.partners:
            if not partner.active:
                continue
            for pool in partner.pools:
                if pool.pool_id == pool_id and pool.network == network and pool.active:
                    fee_type = "core" if is_core else "non_core"
                    if fee_type in partner.fee_allocations:
                        return (partner.name, partner.fee_allocations[fee_type])
        return None