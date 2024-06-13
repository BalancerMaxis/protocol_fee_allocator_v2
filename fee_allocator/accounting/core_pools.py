from __future__ import annotations
from dataclasses import dataclass, field
from web3 import Web3
from typing import List, Dict, TYPE_CHECKING
from decimal import Decimal

from bal_tools.models import PoolSnapshot, TWAPResult
from fee_allocator.accounting.interfaces import AbstractCorePool
from fee_allocator.accounting.decorators import return_zero_if_dust


if TYPE_CHECKING:
    from fee_allocator.accounting.chains import Chain


@dataclass
class CorePool(AbstractCorePool):
    chain: Chain
    pool_id: str
    label: str
    bpt_price: Decimal
    tokens_price: List[TWAPResult]
    gauge_address: str
    start_snap: PoolSnapshot = field(default_factory=PoolSnapshot)
    end_snap: PoolSnapshot = field(default_factory=PoolSnapshot)

    def __post_init__(self):
        self.tokens_price.sort(key=lambda x: x.address)
        self.start_snap.tokens.sort(key=lambda x: x.address)
        self.end_snap.tokens.sort(key=lambda x: x.address)

    @property
    def address(self) -> str:
        return Web3.to_checksum_address(self.pool_id[:42])

    @property
    def earned_bpt_fee(self) -> Decimal:
        return (
            self.end_snap.totalProtocolFeePaidInBPT
            - self.start_snap.totalProtocolFeePaidInBPT
        )

    @property
    @return_zero_if_dust()
    def earned_bpt_fee_usd(self) -> Decimal:
        return self.bpt_price * self.earned_bpt_fee

    @property
    def earned_tokens_fee(self) -> Dict[str, Decimal]:
        return {
            end_token.address: Decimal(
                end_token.paidProtocolFees - start_token.paidProtocolFees
            )
            for start_token, end_token in zip(
                self.start_snap.tokens, self.end_snap.tokens
            )
        }

    @property
    def earned_tokens_fee_usd(self) -> Decimal:
        total_usd = Decimal(0)
        for fee, token in zip(self.earned_tokens_fee.values(), self.tokens_price):
            if fee > 0:
                total_usd += token.twap_price * fee
        return total_usd

    @property
    def total_earned_fees_usd(self) -> Decimal:
        return self.earned_bpt_fee_usd + self.earned_tokens_fee_usd

    @property
    @return_zero_if_dust()
    def earned_fee_share_of_chain_usd(self) -> Decimal:
        return self.total_earned_fees_usd / self.chain.total_earned_fees_usd

    @property
    @return_zero_if_dust(any_or_all="all")
    def total_to_incentives_usd(self) -> Decimal:
        distributed_fees = self.chain.fees_collected * (
            1
            - self.chain.fee_config.dao_share_pct
            - self.chain.fee_config.vebal_share_pct
        )
        return self.earned_fee_share_of_chain_usd * distributed_fees

    @property
    @return_zero_if_dust()
    def to_aura_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * self.chain.aura_vebal_share

    @property
    @return_zero_if_dust()
    def to_bal_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * (1 - self.chain.aura_vebal_share)

    @property
    @return_zero_if_dust()
    def to_dao_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.fee_config.dao_share_pct
        )

    @property
    @return_zero_if_dust()
    def to_vebal_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.fee_config.vebal_share_pct
        )
