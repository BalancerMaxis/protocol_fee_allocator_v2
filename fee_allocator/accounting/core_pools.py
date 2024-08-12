from __future__ import annotations
from dataclasses import dataclass
from web3 import Web3
from typing import List, Dict, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

from bal_tools.models import PoolSnapshot, TWAPResult
from fee_allocator.accounting.interfaces import AbstractCorePool
from fee_allocator.accounting.overrides import CorePoolOverride, overrides

if TYPE_CHECKING:
    from fee_allocator.accounting.chains import Chain


@dataclass
class CorePoolData:
    pool_id: str
    label: str
    bpt_price: Decimal
    tokens_price: List[TWAPResult]
    gauge_address: str
    start_snap: PoolSnapshot
    end_snap: PoolSnapshot


class CorePool(AbstractCorePool):
    def __init__(self, data: CorePoolData, chain: Chain):
        self.data = data
        self.chain = chain
        self.label = data.label
        self.pool_id = data.pool_id
        self.gauge_address = self.data.gauge_address
        self.address = self._set_address()
        self.earned_bpt_fee = self._set_earned_bpt_fee()
        self.earned_bpt_fee_usd = self._set_earned_bpt_fee_usd()
        self.earned_tokens_fee = self._set_earned_tokens_fee()
        self.earned_tokens_fee_usd = self._set_earned_tokens_fee_usd()
        self.total_earned_fees_usd = self._set_total_earned_fees_usd()
        self.last_join_exit_ts = self._set_last_join_exit_ts()

        self.earned_fee_share_of_chain_usd = Decimal(0)
        self.total_to_incentives_usd = Decimal(0)
        self.to_aura_incentives_usd = Decimal(0)
        self.to_bal_incentives_usd = Decimal(0)
        self.redirected_incentives_usd = Decimal(0)
        self.to_dao_usd = Decimal(0)
        self.to_vebal_usd = Decimal(0)

        override_cls = overrides.get(self.pool_id)
        self.override = override_cls(self) if override_cls else None

    def _set_address(self) -> str:
        return Web3.to_checksum_address(self.pool_id[:42])

    def _set_earned_bpt_fee(self) -> Decimal:
        return (
            self.data.end_snap.totalProtocolFeePaidInBPT
            - self.data.start_snap.totalProtocolFeePaidInBPT
        )

    def _set_earned_bpt_fee_usd(self) -> Decimal:
        return self.data.bpt_price * self.earned_bpt_fee

    def _set_earned_tokens_fee(self) -> Dict[str, Decimal]:
        return {
            end_token.address: Decimal(
                end_token.paidProtocolFees - start_token.paidProtocolFees
            )
            for start_token, end_token in zip(
                self.data.start_snap.tokens, self.data.end_snap.tokens
            )
        }

    def _set_earned_tokens_fee_usd(self) -> Decimal:
        return sum(
            token.twap_price * fee
            for fee, token in zip(
                self.earned_tokens_fee.values(), self.data.tokens_price
            )
            if fee > 0
        )

    def _set_total_earned_fees_usd(self) -> Decimal:
        return self.earned_bpt_fee_usd + self.earned_tokens_fee_usd

    def _set_last_join_exit_ts(self) -> str:
        timestamp = self.chain.bal_pools_gauges.get_last_join_exit(self.pool_id)
        gmt_time = datetime.utcfromtimestamp(timestamp)
        return gmt_time.strftime("%Y-%m-%d %H:%M:%S") + "+00:00"

    def update_chain_dependent_values(self, chain_total_earned_fees_usd: Decimal):
        self.earned_fee_share_of_chain_usd = (
            self._calculate_earned_fee_share_of_chain_usd(chain_total_earned_fees_usd)
        )
        self.total_to_incentives_usd = self._calculate_total_to_incentives_usd()
        self.to_aura_incentives_usd = self._calculate_to_aura_incentives_usd()
        self.to_bal_incentives_usd = self._calculate_to_bal_incentives_usd()
        self.to_dao_usd = self._calculate_to_dao_usd()
        self.to_vebal_usd = self._calculate_to_vebal_usd()

    def _calculate_earned_fee_share_of_chain_usd(
        self, chain_total_earned_fees_usd: Decimal
    ) -> Decimal:
        if chain_total_earned_fees_usd == 0:
            return Decimal(0)
        return self.total_earned_fees_usd / chain_total_earned_fees_usd

    def _calculate_total_to_incentives_usd(self) -> Decimal:
        to_distribute_to_incentives = self.chain.fees_collected * (
            1
            - self.chain.fee_config.dao_share_pct
            - self.chain.fee_config.vebal_share_pct
        )
        return self.earned_fee_share_of_chain_usd * to_distribute_to_incentives

    def _calculate_to_aura_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * self.chain.aura_vebal_share

    def _calculate_to_bal_incentives_usd(self) -> Decimal:
        return self.total_to_incentives_usd * (1 - self.chain.aura_vebal_share)

    def _calculate_to_dao_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.fee_config.dao_share_pct
        )

    def _calculate_to_vebal_usd(self) -> Decimal:
        return (
            self.earned_fee_share_of_chain_usd
            * self.chain.fees_collected
            * self.chain.fee_config.vebal_share_pct
        )
