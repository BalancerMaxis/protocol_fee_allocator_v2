from __future__ import annotations
from dataclasses import dataclass, field
from web3 import Web3
from typing import List, Dict, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

from bal_tools.models import PoolSnapshot, TWAPResult
from fee_allocator.accounting.interfaces import AbstractCorePool
from fee_allocator.accounting.overrides import CorePoolOverride, overrides

if TYPE_CHECKING:
    from fee_allocator.accounting.corepoolrunconfig import CorePoolChain


@dataclass
class PoolFeeData:
    pool_id: str
    label: str
    bpt_price: Decimal
    tokens_price: List[TWAPResult]
    gauge_address: str
    start_snap: PoolSnapshot
    end_snap: PoolSnapshot
    last_join_exit_ts: int

    address: str = field(init=False)
    earned_bpt_fee: Decimal = field(init=False)
    earned_bpt_fee_usd: Decimal = field(init=False)
    earned_tokens_fee: Dict[str, Decimal] = field(init=False)
    earned_tokens_fee_usd: Decimal = field(init=False)
    total_earned_fees_usd: Decimal = field(init=False)

    def __post_init__(self):
        self.address = self._set_address()
        self.earned_bpt_fee = self._set_earned_bpt_fee()
        self.earned_bpt_fee_usd = self._set_earned_bpt_fee_usd()
        self.earned_tokens_fee = self._set_earned_tokens_fee()
        self.earned_tokens_fee_usd = self._set_earned_tokens_fee_usd()
        self.total_earned_fees_usd = self._set_total_earned_fees_usd()

    def _set_address(self) -> str:
        return Web3.to_checksum_address(self.pool_id[:42])

    def _set_earned_bpt_fee(self) -> Decimal:
        return (
            self.end_snap.totalProtocolFeePaidInBPT
            - self.start_snap.totalProtocolFeePaidInBPT
        )

    def _set_earned_bpt_fee_usd(self) -> Decimal:
        return self.bpt_price * self.earned_bpt_fee

    def _set_earned_tokens_fee(self) -> Dict[str, Decimal]:
        return {
            end_token.address: Decimal(
                end_token.paidProtocolFees - start_token.paidProtocolFees
            )
            for start_token, end_token in zip(
                self.start_snap.tokens, self.end_snap.tokens
            )
        }

    def _set_earned_tokens_fee_usd(self) -> Decimal:
        return sum(
            token.twap_price * fee
            for fee, token in zip(self.earned_tokens_fee.values(), self.tokens_price)
            if fee > 0
        )

    def _set_total_earned_fees_usd(self) -> Decimal:
        return self.earned_bpt_fee_usd + self.earned_tokens_fee_usd


class PoolFee(AbstractCorePool, PoolFeeData):
    def __init__(self, data: PoolFeeData, chain: CorePoolChain):
        # copy over CorePoolData attributes to self
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
        if self.chain.total_earned_fees_usd == 0:
            return Decimal(0)
        return self.total_earned_fees_usd / self.chain.total_earned_fees_usd

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
