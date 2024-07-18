from __future__ import annotations
from dataclasses import dataclass, field
import os
from typing import List, Dict
from decimal import Decimal
from pathlib import Path

from web3 import Web3
import requests
from bal_tools import Subgraph, BalPoolsGauges
from dotenv import load_dotenv
import joblib
from bal_tools.subgraph import DateRange
from bal_tools.models import PoolSnapshot, Pool
from bal_addresses import AddrBook

from fee_allocator.accounting.core_pools import CorePool
from fee_allocator.accounting.interfaces import AbstractChain
from fee_allocator.accounting.models import FeeConfig, RawCorePoolData, RerouteConfig
from fee_allocator.constants import (
    FEE_CONSTANTS_URL,
    CORE_POOLS_URL,
    REROUTE_CONFIG_URL,
)
from fee_allocator.accounting.decorators import round
from fee_allocator.logger import logger
from fee_allocator.utils import get_block_by_ts


CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

load_dotenv()


@dataclass
class Chain(AbstractChain):
    name: str
    fees_collected: Decimal
    chain_id: int

    subgraph: Subgraph = field(default_factory=Subgraph)
    bal_pools_gauges: BalPoolsGauges = field(default_factory=BalPoolsGauges)
    aura_vebal_share: Decimal = field(default_factory=Decimal)
    core_pools: List[CorePool] = field(default_factory=list)
    web3: Web3 = field(default=None, init=False)

    block_range: DateRange = None
    date_range: DateRange = None
    fee_config: FeeConfig = None

    def __post_init__(self):
        try:
            self.chain_id = AddrBook.chain_ids_by_name[self.name]
        except KeyError:
            raise ValueError(f"chain id for {self.name} not found in `AddrBook`")

        self.fees_collected = Decimal(self.fees_collected)
        self.subgraph = Subgraph(self.name)
        self.bal_pools_gauges = BalPoolsGauges(self.name)

    def initialize_web3(self) -> None:
        rpc_url = os.environ.get(f"{self.name.upper()}NODEURL")
        if not rpc_url:
            raise ValueError(
                f"rpc for {self.name} is not configured.\nexpected '{self.name.upper()}NODEURL' in .env"
            )
        self.web3 = Web3(Web3.HTTPProvider(rpc_url))

    @property
    def total_earned_fees_usd(self) -> Decimal:
        return sum([core_pool.total_earned_fees_usd for core_pool in self.core_pools])

    @property
    def total_earned_fees_over_min_usd(self) -> Decimal:
        return sum([core_pool.redistributed.total_earned_fees_usd for core_pool in self.core_pools])

    @property
    def incentives_to_distribute_per_pool(self) -> Decimal:
        over_min_aura_pools = [
            pool
            for pool in self.core_pools
            if pool.redistributed.base_aura_incentives > self.fee_config.min_aura_incentive
        ]

        if not over_min_aura_pools:
            return Decimal(0)

        debt_to_aura_market = sum(
            [pool.redistributed.debt_to_aura_market for pool in self.core_pools]
        )
        return debt_to_aura_market / len(over_min_aura_pools)


class Chains:
    def __init__(
        self, chain_list: list[Chain], date_range: DateRange, use_cache: bool = True
    ):
        self._chains = {chain.name: chain for chain in chain_list}
        self.date_range = date_range
        self.fee_config = FeeConfig(**requests.get(FEE_CONSTANTS_URL).json())
        self.raw_core_pools = RawCorePoolData(**requests.get(CORE_POOLS_URL).json())
        self.reroute_config = RerouteConfig(**requests.get(REROUTE_CONFIG_URL).json())

        self._set_block_range()
        self._set_aura_vebal_share()
        self._init_core_pools(use_cache)

    def __getattr__(self, name):
        try:
            return self._chains[name]
        except KeyError:
            raise AttributeError(f"Chain {name} is not configured.")

    def _cache_file_path(self, chain: Chain) -> Path:
        filename = f"{chain.name}_{self.date_range[0]}_{self.date_range[1]}.joblib"
        return CACHE_DIR / filename

    def _load_core_pools_from_cache(self, chain: Chain) -> None:
        logger.info(f"loading core pools from cache for {chain.name}")
        core_pools: List[CorePool] = joblib.load(self._cache_file_path(chain))
        chain.core_pools = core_pools
        chain.initialize_web3()

    def _save_core_pools_to_cache(self, chain: Chain) -> None:
        # web3 instance cant be pickled
        chain.web3 = None
        joblib.dump(chain.core_pools, self._cache_file_path(chain))
        chain.initialize_web3()

    def _init_core_pools(self, use_cache: bool) -> None:
        for chain in self._chains.values():
            chain.date_range = self.date_range
            chain.fee_config = self.fee_config

            if use_cache and self._cache_file_path(chain).exists():
                self._load_core_pools_from_cache(chain)
            else:
                self._process_core_pools(chain)
                self._save_core_pools_to_cache(chain)

    def _process_core_pools(self, chain: Chain) -> None:
        logger.info(f"getting snapshots for {chain.name}")
        chain.initialize_web3()

        start_snaps = chain.subgraph.get_balancer_pool_snapshots(
            block=chain.block_range[0], pools_per_req=1000, limit=5000
        )
        end_snaps = chain.subgraph.get_balancer_pool_snapshots(
            block=chain.block_range[1], pools_per_req=1000, limit=5000
        )
        pools = chain.subgraph.fetch_all_pools_info()

        pool_to_gauge = {}
        for pool in pools:
            if pool.gauge.isKilled:
                logger.info(f"{pool.id} gauge:{pool.gauge.address} is killed, skipping")
                continue
            pool_to_gauge[pool.id] = Web3.to_checksum_address(pool.gauge.address)

        for pool_id, label in self.raw_core_pools[chain.name].items():
            if self._should_add_core_pool(chain, pool_id, start_snaps):
                self._add_core_pool(
                    chain, pool_id, label, pool_to_gauge, start_snaps, end_snaps
                )

    def _should_add_core_pool(
        self, chain: Chain, pool_id: str, start_snaps: list[PoolSnapshot]
    ) -> bool:
        snapshot_ids = [snapshot.id for snapshot in start_snaps]
        return (
            pool_id in snapshot_ids
            and chain.bal_pools_gauges.has_alive_preferential_gauge(pool_id)
        )

    def _add_core_pool(
        self,
        chain: Chain,
        pool_id: str,
        label: str,
        pool_to_gauge: Dict[str, str],
        start_snaps: list[PoolSnapshot],
        end_snaps: list[PoolSnapshot],
    ) -> None:
        start_snap = self._get_latest_snapshot(start_snaps, pool_id)
        end_snap = self._get_latest_snapshot(end_snaps, pool_id)

        if start_snap and end_snap:
            logger.info(f"fetching twap prices for {label} on {chain.name}")
            prices = chain.subgraph.get_twap_price_pool(
                pool_id, chain.name, self.date_range, chain.web3, chain.block_range[1]
            )

            chain.core_pools.append(
                CorePool(
                    chain=chain,
                    pool_id=pool_id,
                    label=label,
                    bpt_price=prices.bpt_price,
                    tokens_price=prices.token_prices,
                    gauge_address=pool_to_gauge[pool_id],
                    start_snap=start_snap,
                    end_snap=end_snap,
                )
            )
        else:
            logger.warning(f"No snapshots found for {label} - {pool_id}")

    def _set_aura_vebal_share(self) -> Decimal:
        if not self.mainnet:
            raise ValueError("mainnet is needed to calculate aura vebal share")

        if not self.mainnet.web3:
            self.mainnet.initialize_web3()

        vebal_share = self.mainnet.subgraph.calculate_aura_vebal_share(
            self.mainnet.web3, self.mainnet.block_range[1]
        )
        for chain in self.all_chains:
            chain.aura_vebal_share = vebal_share

    def _set_block_range(self) -> None:
        for chain in self.all_chains:
            start_block = get_block_by_ts(self.date_range[0], chain)
            end_block = get_block_by_ts(self.date_range[1], chain)

            chain.block_range = (start_block, end_block)
            logger.info(f"set blocks for {chain.name}: {start_block} - {end_block}")

    @staticmethod
    def _get_latest_snapshot(
        snapshots: list[PoolSnapshot], pool_id: str
    ) -> PoolSnapshot:
        return next(
            (
                snap
                for snap in sorted(snapshots, key=lambda x: x.timestamp, reverse=True)
                if snap.id == pool_id
            ),
            None,
        )

    @property
    def all_chains(self) -> List[Chain]:
        return list(self._chains.values())

    @property
    @round(4)
    def total_to_dao_usd(self) -> Decimal:
        return sum(
            [pool.to_dao_usd for chain in self.all_chains for pool in chain.core_pools]
        )

    @property
    @round(4)
    def total_to_incentives_usd(self) -> Decimal:
        return sum(
            [
                pool.total_to_incentives_usd + pool.to_dao_usd + pool.to_vebal_usd
                for chain in self.all_chains
                for pool in chain.core_pools
            ]
        )

    @property
    @round(4)
    def total_fees_collected_usd(self) -> Decimal:
        return sum([chain.fees_collected for chain in self.all_chains])
