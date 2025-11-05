from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, TYPE_CHECKING, Optional, Union
from decimal import Decimal

from bal_tools.models import PoolSnapshot, TWAPResult
from fee_allocator.accounting.interfaces import AbstractPoolFee

if TYPE_CHECKING:
    from fee_allocator.accounting.chains import CorePoolChain
    from fee_allocator.accounting.models import AllianceFeeAllocation, PartnerFeeAllocation, Partner


@dataclass
class PoolFeeData:
    """
    Holds pool fee data for a single pool sourced from the subgraph.
    This is also the class that gets cached.

    Args:
        pool_id (str): The unique identifier for the pool.
        symbol (str): The symbol representing the pool.
        bpt_price (Decimal): The price of the BPT.
        tokens_price (List[TWAPResult]): A list of time-weighted average prices for the tokens in the pool.
        gauge_address (str): The address of the gauge associated with the pool.
        start_pool_snapshot (PoolSnapshot): The pool snapshot at the start of the period.
        end_pool_snapshot (PoolSnapshot): The pool snapshot at the end of the period.
        last_join_exit_ts (int): The timestamp of the last join or exit event for the pool.
        protocol_version (int): The protocol version of the pool (2 or 3).
    """
    pool_id: str
    address: str
    symbol: str
    tokens_price: List[TWAPResult]
    gauge_address: Optional[str]
    start_pool_snapshot: Optional[PoolSnapshot]
    end_pool_snapshot: Optional[PoolSnapshot]
    last_join_exit_ts: int
    protocol_version: int
    bpt_price: Decimal = field(default=Decimal(0))
    total_earned_fees_usd_twap: Decimal = None
    pool_category: Optional[str] = None  # "core_with_gauge", "non_core_with_gauge", "non_core_without_gauge"
    fee_config: Optional[Union['AllianceFeeAllocation', 'PartnerFeeAllocation']] = None
    partner: Optional['Partner'] = None
    alliance_member: Optional[str] = None
    is_alliance_pool: bool = field(default=False)
    is_alliance_non_core_pool: bool = field(default=False)

    def __post_init__(self):
        if self.protocol_version == 3:
            if self.total_earned_fees_usd_twap is None:
                raise ValueError(f"v3 pool {self.pool_id} must have total_earned_fees_usd_twap set. got {self.total_earned_fees_usd_twap}")
        elif self.protocol_version == 2:
            self.total_earned_fees_usd_twap = self._set_total_earned_fees_usd_twap_v2()
        else:
            raise ValueError(f"Invalid protocol version {self.protocol_version} for pool {self.pool_id}")

    def _set_total_earned_fees_usd_twap_v2(self) -> Decimal:
        bpt_fee = (
            self.end_pool_snapshot.totalProtocolFeePaidInBPT
            - self.start_pool_snapshot.totalProtocolFeePaidInBPT
        )
        if bpt_fee > 0:
            return self.bpt_price * bpt_fee

        return Decimal(sum(
            token.twap_price * Decimal(end_token.paidProtocolFees - start_token.paidProtocolFees)
            for end_token, start_token, token in zip(
                self.end_pool_snapshot.tokens,
                self.start_pool_snapshot.tokens,
                self.tokens_price
            )
            if end_token.paidProtocolFees > start_token.paidProtocolFees
        ))


class PoolFee(AbstractPoolFee, PoolFeeData):
    """
    Creates an initial fee allocation based on the input `PoolFeeData` for a pool.
    The allocation is also based on properties from its respective `CorePoolChain` such as the fee config and the pool's share of the total fees.

    Args:
        data (PoolFeeData): The pool fee data for initialization.
        chain (CorePoolChain): The core pool chain this pool belongs to.
    """
    def __init__(self, data: PoolFeeData, chain: CorePoolChain):
        self.__dict__.update(vars(data))
        self.chain = chain

        self.is_alliance_pool = self._check_if_alliance_pool()
        self.alliance_fee_config = self._get_alliance_fee_config() if self.is_alliance_pool else None
        self.is_alliance_non_core_pool = self._is_alliance_non_core_pool()
        
        self.is_partner_pool = self._check_if_partner_pool()
        self.partner_info = self._get_partner_info() if self.is_partner_pool else None

        self.voting_pool_override = self._get_voting_pool_override()
        self.market_override = self._get_market_override()
        
        self.original_earned_fee_share = Decimal(0)
        self.earned_fee_share_of_chain_usd = self._earned_fee_share_of_chain_usd()
        self.total_to_incentives_usd = self._total_to_incentives_usd()
        self.to_aura_incentives_usd = self._to_aura_incentives_usd()
        self.to_bal_incentives_usd = self._to_bal_incentives_usd()
        self.to_dao_usd = self._to_dao_usd()
        self.to_vebal_usd = self._to_vebal_usd()
        self.to_partner_usd = self._to_partner_usd()
        self.to_beets_usd = self._to_beets_usd()
        self.redirected_incentives_usd = Decimal(0)

    def _check_if_alliance_pool(self) -> bool:
        in_config = self.chain.chains.alliance_config.get_pool_fee_config(self.pool_id, self.chain.name, True) is not None
        if not in_config:
            return False
        return any(p.pool_id == self.pool_id for p in self.chain.alliance_pools)

    def _get_alliance_fee_config(self):
        return self.chain.chains.alliance_config.get_pool_fee_config(self.pool_id, self.chain.name, True)

    def _is_alliance_non_core_pool(self) -> bool:
        if not self.is_alliance_pool:
            return False
        return self.pool_category != "core_with_gauge"
    
    def _check_if_partner_pool(self) -> bool:
        return self.partner is not None

    @property
    def is_alliance_core_pool(self) -> bool:
        """Returns True if this is an Alliance pool with core type (not non-core)"""
        return self.is_alliance_pool and not self.is_alliance_non_core_pool

    def _get_partner_info(self):
        if self.partner:
            return (self.partner, self.fee_config)
        return None
    
    def _get_voting_pool_override(self):
        pool_override = self.chain.chains.pool_overrides.get(self.pool_id)
        return pool_override.voting_pool_override if pool_override else None
    
    def _get_market_override(self) -> str:
        pool_override = self.chain.chains.pool_overrides.get(self.pool_id)
        return pool_override.market_override if pool_override else "hh"
    

    def _earned_fee_share_of_chain_usd(self) -> Decimal:
        if self.chain.total_earned_fees_usd_twap == 0:
            return Decimal(0)
        return self.total_earned_fees_usd_twap / self.chain.total_earned_fees_usd_twap

    def _core_pool_allocation(self) -> Decimal:
        if self.chain.total_earned_fees_usd_twap == 0:
            return Decimal(0)
        core_share = self.chain.total_earned_fees_usd_twap / self.chain.total_fees_earned
        return self.chain.fees_collected * core_share

    def _total_to_incentives_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()

        if self.fee_config:
            vote_incentive_pct = self.fee_config.vote_incentive_pct
        else:
            vote_incentive_pct = self.chain.chains.fee_config.vote_incentive_pct

        to_distribute_to_incentives = core_fees * vote_incentive_pct
        return self.earned_fee_share_of_chain_usd * to_distribute_to_incentives

    def _calculate_incentive_split(self, platform: str) -> Decimal:
        # Alliance core pools get 100% to AURA
        if self.is_alliance_core_pool:
            return self.total_to_incentives_usd if platform == "aura" else Decimal(0)

        if self.voting_pool_override == "split" or self.voting_pool_override is None:
            aura_share = self.chain.chains.aura_vebal_share
            return self.total_to_incentives_usd * (aura_share if platform == "aura" else (1 - aura_share))

        if self.voting_pool_override == platform:
            return self.total_to_incentives_usd
        elif self.voting_pool_override and self.voting_pool_override != platform:
            return Decimal(0)

        aura_share = self.chain.chains.aura_vebal_share
        return self.total_to_incentives_usd * (aura_share if platform == "aura" else (1 - aura_share))

    def _to_aura_incentives_usd(self) -> Decimal:
        return self._calculate_incentive_split("aura")

    def _to_bal_incentives_usd(self) -> Decimal:
        return self._calculate_incentive_split("bal")

    def _to_dao_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()
        beets_factor = self.chain.get_beets_factor()

        if self.fee_config:
            dao_share_pct = self.fee_config.dao_share_pct
        else:
            dao_share_pct = self.chain.chains.fee_config.dao_share_pct

        return self.earned_fee_share_of_chain_usd * core_fees * dao_share_pct * (1 - beets_factor)

    def _to_vebal_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()
        beets_factor = self.chain.get_beets_factor()

        if self.fee_config:
            vebal_share_pct = self.fee_config.vebal_share_pct
        else:
            vebal_share_pct = self.chain.chains.fee_config.vebal_share_pct

        return self.earned_fee_share_of_chain_usd * core_fees * vebal_share_pct * (1 - beets_factor)

    def _to_partner_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()

        if self.alliance_member or self.partner:
            return (
                self.earned_fee_share_of_chain_usd
                * core_fees
                * self.fee_config.partner_share_pct
            )
        return Decimal(0)
        
    def _to_beets_usd(self) -> Decimal:
        beets_factor = self.chain.get_beets_factor()
        if beets_factor == 0:
            return Decimal(0)
        # Beets receives the portion that was deducted from DAO and veBAL
        core_fees = self._core_pool_allocation()

        if self.fee_config:
            dao_share_pct = self.fee_config.dao_share_pct
            vebal_share_pct = self.fee_config.vebal_share_pct
        else:
            dao_share_pct = self.chain.chains.fee_config.dao_share_pct
            vebal_share_pct = self.chain.chains.fee_config.vebal_share_pct

        return self.earned_fee_share_of_chain_usd * core_fees * (dao_share_pct + vebal_share_pct) * beets_factor