from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, TYPE_CHECKING
from decimal import Decimal

from bal_tools.models import PoolSnapshot, TWAPResult
from fee_allocator.accounting.interfaces import AbstractPoolFee
from fee_allocator.accounting.overrides import PoolFeeOverride, overrides

if TYPE_CHECKING:
    from fee_allocator.accounting.chains import CorePoolChain


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
    gauge_address: str
    start_pool_snapshot: PoolSnapshot
    end_pool_snapshot: PoolSnapshot
    last_join_exit_ts: int
    protocol_version: int
    bpt_price: Decimal = field(default=Decimal(0))
    total_earned_fees_usd_twap: Decimal = None
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
        # copy over PoolFeeData attributes to self
        self.__dict__.update(vars(data))
        self.chain = chain

        self.is_alliance_pool = self._check_if_alliance_pool()
        self.alliance_fee_config = self._get_alliance_fee_config() if self.is_alliance_pool else None
        self.is_alliance_non_core_pool = self._is_alliance_non_core_pool()

        self.original_earned_fee_share = Decimal(0)
        self.earned_fee_share_of_chain_usd = self._earned_fee_share_of_chain_usd()
        self.total_to_incentives_usd = self._total_to_incentives_usd()
        self.to_aura_incentives_usd = self._to_aura_incentives_usd()
        self.to_bal_incentives_usd = self._to_bal_incentives_usd()
        self.to_dao_usd = self._to_dao_usd()
        self.to_vebal_usd = self._to_vebal_usd()
        self.to_partner_usd = self._to_partner_usd()
        self.redirected_incentives_usd = Decimal(0)

        override_cls = overrides.get(self.pool_id)
        self.override = override_cls(self) if override_cls else None

    
    def _check_if_alliance_pool(self) -> bool:
        return self.chain.chains.alliance_config.get_pool_fee_config(self.pool_id, self.chain.name, True) is not None

    def _get_alliance_fee_config(self):
        return self.chain.chains.alliance_config.get_pool_fee_config(self.pool_id, self.chain.name, True)

    def _is_alliance_non_core_pool(self) -> bool:
        if not self.is_alliance_pool:
            return False

        for member in self.chain.chains.alliance_config.alliance_members:
            for pool in member.pools:
                if pool.pool_type != "core":
                    print(f"Alliance non-core pool: {pool.pool_id} {pool.network} {pool.active}")
                if pool.pool_id == self.pool_id and pool.network == self.chain.name and pool.active:
                    return pool.pool_type != "core"
        return False

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
        vote_incentive_pct = self.chain.chains.alliance_config.alliance_fee_allocations["core"].vote_incentive_pct if self.is_alliance_pool else self.chain.chains.fee_config.vote_incentive_pct
        to_distribute_to_incentives = core_fees * vote_incentive_pct
        return self.earned_fee_share_of_chain_usd * to_distribute_to_incentives

    def _to_aura_incentives_usd(self) -> Decimal:
        if self.is_alliance_non_core_pool:
            return self.total_to_incentives_usd
        return self.total_to_incentives_usd * self.chain.chains.aura_vebal_share

    def _to_bal_incentives_usd(self) -> Decimal:
        if self.is_alliance_non_core_pool:
            return Decimal(0)
        return self.total_to_incentives_usd * (1 - self.chain.chains.aura_vebal_share)

    def _to_dao_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()
        return (
            self.earned_fee_share_of_chain_usd
            * core_fees
            * (self.chain.chains.fee_config.dao_share_pct if not self.is_alliance_non_core_pool else self.alliance_fee_config.dao_share_pct)
        )

    def _to_vebal_usd(self) -> Decimal:
        core_fees = self._core_pool_allocation()
        return (
            self.earned_fee_share_of_chain_usd
            * core_fees
            * (self.chain.chains.fee_config.vebal_share_pct if not self.is_alliance_non_core_pool else self.alliance_fee_config.vebal_share_pct)
        )

    def _to_partner_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.total_earned_fees_usd_twap
            * self.alliance_fee_config.partner_share_pct
        ) if self.is_alliance_pool else Decimal(0)