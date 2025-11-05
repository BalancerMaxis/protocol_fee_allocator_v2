from pydantic import BaseModel, validator
from decimal import Decimal
from typing import Dict, NewType, Optional


Pools = Dict[NewType("PoolId", str), NewType("Symbol", str)]
InputFees = Dict[NewType("CorePoolChainName", str), NewType("FeesCollected", int)]


class PoolOverride(BaseModel):
    """
    Represents pool-specific overrides for voting pool and market platforms.
    """
    voting_pool_override: Optional[str] = None  # "bal" or "aura"
    market_override: str = "hh"  # "hh" (HiddenHand) or "paladin" (Paladin Quest)


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
    
    @validator('dao_share_pct')
    def validate_percentages(cls, v, values):
        # For core pools (vote_incentive_pct is not None), all percentages must sum to 1
        if 'vote_incentive_pct' in values and values['vote_incentive_pct'] is not None:
            total = values['vote_incentive_pct'] + values.get('vebal_share_pct', 0) + values.get('partner_share_pct', 0) + v
            if abs(total - Decimal('1')) > Decimal('0.0001'):
                raise ValueError(f'Fee percentages must sum to 100%, got {total * 100}%')
        # For non-core pools, vebal + partner + dao must sum to 1
        elif 'vebal_share_pct' in values and 'partner_share_pct' in values:
            total = values['vebal_share_pct'] + values['partner_share_pct'] + v
            if abs(total - Decimal('1')) > Decimal('0.0001'):
                raise ValueError(f'Fee percentages must sum to 100%, got {total * 100}%')
        return v


class AllianceThresholds(BaseModel):
    """
    Represents threshold values for Alliance pool eligibility.
    """
    v3_min_tvl: Decimal
    v2_min_tvl: Decimal


class PartnerFeeAllocation(BaseModel):
    """
    Represents the fee allocation configuration for Partner pools.
    """
    vebal_share_pct: Decimal
    vote_incentive_pct: Decimal | None = None  # Only for core pools
    partner_share_pct: Decimal
    dao_share_pct: Decimal

    @validator('dao_share_pct')
    def validate_percentages(cls, v, values):
        # For core pools (vote_incentive_pct is not None), all percentages must sum to 1
        if 'vote_incentive_pct' in values and values['vote_incentive_pct'] is not None:
            total = values['vote_incentive_pct'] + values.get('vebal_share_pct', 0) + values.get('partner_share_pct', 0) + v
            if abs(total - Decimal('1')) > Decimal('0.0001'):
                raise ValueError(f'Fee percentages must sum to 100%, got {total * 100}%')
        # For non-core pools, vebal + partner + dao must sum to 1
        elif 'vebal_share_pct' in values and 'partner_share_pct' in values:
            total = values['vebal_share_pct'] + values['partner_share_pct'] + v
            if abs(total - Decimal('1')) > Decimal('0.0001'):
                raise ValueError(f'Fee percentages must sum to 100%, got {total * 100}%')
        return v


class PartnerFeeAllocations(BaseModel):
    core_with_gauge: PartnerFeeAllocation
    non_core_with_gauge: PartnerFeeAllocation
    non_core_without_gauge: PartnerFeeAllocation


class Partner(BaseModel):
    """
    Represents a partner in the fee sharing program.
    Partners are distinct from Alliance members - they receive custom fee splits.
    """
    name: str
    multisig_address: str
    active: bool
    pool_types: Optional[list[str]] = None  # List of pool types this partner supports (e.g., ["QUANT_AMM_WEIGHTED"])
    pools: Optional[list[str]] = None
    # Optional custom fee allocations - if not specified, uses default from PartnerConfig
    fee_allocations: PartnerFeeAllocations | None = None

    @validator('pools')
    def validate_has_pool_source(cls, v, values):
        """Ensure partner has either pools or pool_types defined"""
        if not v and not values.get('pool_types'):
            raise ValueError(f"Partner {values.get('name', 'unknown')} must have either 'pools' or 'pool_types' defined")
        return v


class AllianceConfig(BaseModel):
    """
    Represents the Alliance configuration including members and fee allocations.
    Models the data sourced from the ALLIANCE_CONFIG_URL endpoint.
    """
    alliance_members: list[AllianceMember]
    alliance_fee_allocations: dict[str, AllianceFeeAllocation]
    alliance_thresholds: AllianceThresholds

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


class PartnerConfig(BaseModel):
    """
    Represents the Partner configuration including partners and their fee allocations.
    Models the data sourced from the PARTNER_CONFIG_URL endpoint.
    """
    partners: list[Partner]
    default_fee_allocations: PartnerFeeAllocations

    def get_partner_fee_config(self, partner_name: str, pool_type: str) -> PartnerFeeAllocation:
        """
        Returns the fee allocation for a partner and pool type.
        Falls back to default if partner doesn't have custom allocations.

        Args:
            partner_name: Name of the partner
            pool_type: One of "core_with_gauge", "non_core_with_gauge", "non_core_without_gauge"
        """
        for partner in self.partners:
            if partner.name == partner_name and partner.active:
                if partner.fee_allocations:
                    return getattr(partner.fee_allocations, pool_type)
                else:
                    return getattr(self.default_fee_allocations, pool_type)
        return getattr(self.default_fee_allocations, pool_type)
    
