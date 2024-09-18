from __future__ import annotations
from dataclasses import dataclass, field
from web3 import Web3
from typing import List, Dict, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

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
    """
    pool_id: str
    symbol: str
    bpt_price: Decimal
    tokens_price: List[TWAPResult]
    gauge_address: str
    start_pool_snapshot: PoolSnapshot
    end_pool_snapshot: PoolSnapshot
    last_join_exit_ts: int

    address: str = field(init=False)
    earned_bpt_fee: Decimal = field(init=False)
    earned_bpt_fee_usd_twap: Decimal = field(init=False)
    earned_tokens_fee: Dict[str, Decimal] = field(init=False)
    earned_tokens_fee_usd_twap: Decimal = field(init=False)
    total_earned_fees_usd_twap: Decimal = field(init=False)

    def __post_init__(self):
        self.address = self._set_address()
        self.earned_bpt_fee = self._set_earned_bpt_fee()
        self.earned_bpt_fee_usd_twap = self._set_earned_bpt_fee_usd_twap()
        self.earned_tokens_fee = self._set_earned_tokens_fee()
        self.earned_tokens_fee_usd_twap = self._set_earned_tokens_fee_usd_twap()
        self.total_earned_fees_usd_twap = self._set_total_earned_fees_usd_twap()

    def _set_address(self) -> str:
        return Web3.to_checksum_address(self.pool_id[:42])

    def _set_earned_bpt_fee(self) -> Decimal:
        return (
            self.end_pool_snapshot.totalProtocolFeePaidInBPT
            - self.start_pool_snapshot.totalProtocolFeePaidInBPT
        )

    def _set_earned_bpt_fee_usd_twap(self) -> Decimal:
        return self.bpt_price * self.earned_bpt_fee

    def _set_earned_tokens_fee(self) -> Dict[str, Decimal]:
        return {
            end_token.address: Decimal(
                end_token.paidProtocolFees - start_token.paidProtocolFees
            )
            for start_token, end_token in zip(
                self.start_pool_snapshot.tokens, self.end_pool_snapshot.tokens
            )
        }

    def _set_earned_tokens_fee_usd_twap(self) -> Decimal:
        return sum(
            token.twap_price * fee
            for fee, token in zip(self.earned_tokens_fee.values(), self.tokens_price)
            if fee > 0
        )

    def _set_total_earned_fees_usd_twap(self) -> Decimal:
        return self.earned_bpt_fee_usd_twap + self.earned_tokens_fee_usd_twap


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

        self.earned_fee_share_of_chain_usd = self._earned_fee_share_of_chain_usd()
        self.total_to_incentives_usd = self._total_to_incentives_usd()
        self.to_aura_incentives_usd = self._to_aura_incentives_usd()
        self.to_bal_incentives_usd = self._to_bal_incentives_usd()
        self.to_dao_usd = self._to_dao_usd()
        self.to_vebal_usd = self._to_vebal_usd()
        self.redirected_incentives_usd = Decimal(0)

        override_cls = overrides.get(self.pool_id)
        self.override = override_cls(self) if override_cls else None

    def _earned_fee_share_of_chain_usd(self) -> Decimal:
        if self.chain.total_earned_fees_usd_twap == 0:
            return Decimal(0)
        return self.total_earned_fees_usd_twap / self.chain.total_earned_fees_usd_twap

    def _total_to_incentives_usd(self) -> Decimal:
        to_distribute_to_incentives = self.chain.fees_collected * (
            1
            - self.chain.chains.fee_config.dao_share_pct
            - self.chain.chains.fee_config.vebal_share_pct
        )
        return self.earned_fee_share_of_chain_usd * to_distribute_to_incentives

    def _to_aura_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * self.chain.chains.aura_vebal_share

    def _to_bal_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * (1 - self.chain.chains.aura_vebal_share)

    def _to_dao_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.chains.fee_config.dao_share_pct
        )

    def _to_vebal_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.chains.fee_config.vebal_share_pct
        )
