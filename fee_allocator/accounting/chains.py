from __future__ import annotations
from typing import List, Dict
from decimal import Decimal
from pathlib import Path
import os
from dotenv import load_dotenv

from web3 import Web3
import requests
from bal_tools import Subgraph, BalPoolsGauges, Web3RpcByChain
import joblib
from bal_tools.subgraph import DateRange
from bal_tools.models import PoolSnapshot, Pool
from bal_addresses import AddrBook

from fee_allocator.accounting.core_pools import CorePool, CorePoolData
from fee_allocator.accounting.interfaces import AbstractChain
from fee_allocator.accounting.models import (
    FeeConfig,
    RawCorePoolData,
    RerouteConfig,
    RawPools,
    InputFees,
)
from fee_allocator.constants import (
    FEE_CONSTANTS_URL,
    CORE_POOLS_URL,
    REROUTE_CONFIG_URL,
)
from fee_allocator.accounting.decorators import round
from fee_allocator.logger import logger
from fee_allocator.utils import get_block_by_ts


load_dotenv()


class Chains:
    def __init__(
        self,
        input_fees: InputFees,
        date_range: DateRange,
        cache_dir: Path = None,
        use_cache: bool = True,
    ):
        self.input_fees = input_fees
        self.date_range = date_range
        self.w3_by_chain = Web3RpcByChain(os.environ["DRPC_KEY"])

        self.raw_core_pools = RawCorePoolData(**requests.get(CORE_POOLS_URL).json())
        self.fee_config = FeeConfig(**requests.get(FEE_CONSTANTS_URL).json())
        self.reroute_config = RerouteConfig(**requests.get(REROUTE_CONFIG_URL).json())

        self.use_cache = use_cache
        self.cache_dir = cache_dir if cache_dir else Path(__file__).parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)

        self._chains = self._init_chains()
        self.aura_vebal_share = self._set_aura_vebal_share()

        # CorePools can only be init after all Chains/Chain data is set
        self._set_core_pools()

    def __getattr__(self, name):
        try:
            return self._chains[name]
        except KeyError:
            raise AttributeError(f"Chain {name} is not configured.")

    def _init_chains(self) -> dict[str, Chain]:
        _chains = {}
        for chain, fees in self.input_fees.items():
            _chains[chain] = Chain(self, chain, fees, self.w3_by_chain[chain])
        return _chains

    def _set_aura_vebal_share(self) -> Decimal:
        if not self.mainnet:
            raise ValueError(
                "mainnet must be initialized to calculate aura vebal share"
            )

        return self.mainnet.subgraph.calculate_aura_vebal_share(
            self.mainnet.web3, self.mainnet.block_range[1]
        )

    def _set_core_pools(self) -> None:
        for chain in self.all_chains:
            chain.core_pools = [CorePool(data, chain) for data in chain.core_pool_data]

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


class Chain(AbstractChain):
    def __init__(self, chains: Chains, name: str, fees: int, web3: Web3):
        self.chains = chains
        self.name = name
        self.fees_collected = fees
        self.web3 = web3

        try:
            self.chain_id = AddrBook.chain_ids_by_name[self.name]
        except KeyError:
            raise ValueError(f"chain id for {self.name} not found in `AddrBook`")

        self.fees_collected = Decimal(self.fees_collected)
        self.subgraph = Subgraph(self.name)
        self.bal_pools_gauges = BalPoolsGauges(self.name)

        self.block_range = self._set_block_range()
        self.core_pool_data = self._init_core_pool_data()
        self.core_pools: List[CorePool] = []

    def _set_block_range(self) -> tuple[int, int]:
        start = get_block_by_ts(self.chains.date_range[0], self)
        end = get_block_by_ts(self.chains.date_range[1], self)
        logger.info(f"set blocks for {self.name}: {start} - {end}")
        return (start, end)

    def _init_core_pool_data(self) -> list[CorePoolData]:
        if self.chains.use_cache and self._cache_file_exists():
            pool_data = self._load_core_pools_from_cache()
        else:
            pool_data = self._fetch_and_process_core_pool_data()
            self._save_core_pools_to_cache(pool_data)

        return pool_data

    def _cache_file_exists(self) -> bool:
        return self._cache_file_path().exists()

    def _cache_file_path(self) -> Path:
        filename = f"{self.name}_{self.chains.date_range[0]}_{self.chains.date_range[1]}.joblib"
        return self.chains.cache_dir / filename

    def _load_core_pools_from_cache(self) -> list[CorePoolData]:
        logger.info(f"loading core pools from cache for {self.name}")
        return joblib.load(self._cache_file_path())

    def _save_core_pools_to_cache(self, pool_data: list[CorePoolData]) -> None:
        joblib.dump(pool_data, self._cache_file_path())

    def _fetch_and_process_core_pool_data(self) -> list[CorePoolData]:
        logger.info(f"getting snapshots for {self.name}")

        start_snaps = self.subgraph.get_balancer_pool_snapshots(
            block=self.block_range[0], pools_per_req=1000, limit=5000
        )
        end_snaps = self.subgraph.get_balancer_pool_snapshots(
            block=self.block_range[1], pools_per_req=1000, limit=5000
        )
        pools = self.subgraph.fetch_all_pools_info()
        pool_to_gauge = self._create_pool_to_gauge_mapping(pools)

        pool_data = []

        for pool_id, label in self.chains.raw_core_pools[self.name].items():
            if self._should_add_core_pool(pool_id, start_snaps):
                data =  self._get_core_pool_data(pool_id, label, pool_to_gauge, start_snaps, end_snaps)
                if data:
                    pool_data.append(data)

        return pool_data

    def _create_pool_to_gauge_mapping(self, pools: list[Pool]) -> Dict[str, str]:
        pool_to_gauge = {}
        for pool in pools:
            if pool.gauge.isKilled:
                logger.info(
                    f"gauge {pool.gauge.address} (pool id: {pool.id}) is killed, skipping"
                )
                continue
            pool_to_gauge[pool.id] = Web3.to_checksum_address(pool.gauge.address)
        return pool_to_gauge

    def _should_add_core_pool(
        self, pool_id: str, start_snaps: list[PoolSnapshot]
    ) -> bool:
        snapshot_ids = [snapshot.id for snapshot in start_snaps]
        return (
            pool_id in snapshot_ids
            and self.bal_pools_gauges.has_alive_preferential_gauge(pool_id)
        )

    def _get_core_pool_data(
        self,
        pool_id: str,
        label: str,
        pool_to_gauge: Dict[str, str],
        start_snaps: list[PoolSnapshot],
        end_snaps: list[PoolSnapshot],
    ) -> CorePoolData:
        start_snap = self._get_latest_snapshot(start_snaps, pool_id)
        end_snap = self._get_latest_snapshot(end_snaps, pool_id)

        if start_snap and end_snap:
            logger.info(f"fetching twap prices for {label} on {self.name}")
            prices = self.subgraph.get_twap_price_pool(
                pool_id,
                self.name,
                self.chains.date_range,
                self.web3,
                self.block_range[1],
            )

            return CorePoolData(
                pool_id=pool_id,
                label=label,
                bpt_price=prices.bpt_price,
                tokens_price=prices.token_prices,
                gauge_address=pool_to_gauge[pool_id],
                start_snap=start_snap,
                end_snap=end_snap,
                last_join_exit_ts=self.bal_pools_gauges.get_last_join_exit(pool_id),
            )
        else:
            logger.warning(f"No snapshots found for {label} - {pool_id}")
            return None

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
    def total_earned_fees_usd(self) -> Decimal:
        return sum(
            [pool_data.total_earned_fees_usd for pool_data in self.core_pool_data]
        )
