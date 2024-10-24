from __future__ import annotations
from typing import List, Dict, Union
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

from fee_allocator.accounting.core_pools import PoolFee, PoolFeeData
from fee_allocator.accounting.interfaces import AbstractCorePoolChain
from fee_allocator.accounting.models import (
    GlobalFeeConfig,
    RerouteConfig,
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


class CorePoolRunConfig:
    """
    Initializes chain agnostic data and properties based on aggregated chain data.
    Contains methods to setup a `CorePoolChain` for each chain defined in `input_fees`.

    Args:
        input_fees (InputFees): A dictionary of chain names to fee amounts.
        date_range (DateRange): The date range for the fee allocation period.
        cache_dir (Path, optional): The directory to use for caching. Defaults to fee_allocator/cache.
        use_cache (bool, optional): Whether to use cached data. Defaults to True.
    """
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

        self.fee_config = GlobalFeeConfig(**requests.get(FEE_CONSTANTS_URL).json())
        self.reroute_config = RerouteConfig(**requests.get(REROUTE_CONFIG_URL).json())

        # caches a list of `PoolFeeData` for each chain
        self.use_cache = use_cache
        self.cache_dir = cache_dir if cache_dir else Path(__file__).parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)

        self._chains: Union[dict[str, CorePoolChain], None] = None
        self.aura_vebal_share: Union[Decimal, None] = None


    def __getattr__(self, name):
        try:
            return self._chains[name]
        except KeyError:
            return getattr(self, name)

    def set_core_pool_chains_data(self):
        """
        iterate over each chain in `input_fees` and fetch that chain's core pool data
        """
        _chains = {}
        for chain_name, fees in self.input_fees.items():
            chain = CorePoolChain(self, chain_name, fees, self.w3_by_chain[chain_name])
            chain.set_pool_fee_data()
            _chains[chain_name] = chain

        self._chains = _chains

    def set_aura_vebal_share(self):
        if not self.mainnet:
            raise ValueError(
                "mainnet must be initialized to calculate aura vebal share"
            )

        self.aura_vebal_share = self.mainnet.subgraph.calculate_aura_vebal_share(
            self.mainnet.web3, self.mainnet.block_range[1]
        )

    def set_initial_pool_allocation(self) -> None:
        """
        sets the intial fee allocation for all pools for all chains
        """
        if not self._chains:
            logger.warning("Core pool chains data not set, fetching core pool chains data")
            self.set_core_pool_chains_data()

        for chain in self.all_chains:
            chain.core_pools = [PoolFee(data, chain) for data in chain.pool_fee_data]

    @property
    def all_chains(self) -> List[CorePoolChain]:
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


class CorePoolChain(AbstractCorePoolChain):
    """
    Initializes chain specific data/tools based on the input chain `name`.
    Contains methods for fetching and processing a list of `PoolFeeData`/`PoolFee` for this chain.
    Also handles caching pool data.

    Args:
        chains (CorePoolRunConfig): The parent CorePoolRunConfig instance.
        name (str): The name of the chain.
        fees (int): The total fees collected for this chain.
        web3 (Web3): The Web3 instance for this chain.
    """
    def __init__(self, chains: CorePoolRunConfig, name: str, fees: int, web3: Web3):
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
        self.pool_fee_data: Union[list[PoolFeeData], None] = None
        self.core_pools: List[PoolFee] = []

    def _set_block_range(self) -> tuple[int, int]:
        start = get_block_by_ts(self.chains.date_range[0], self)
        end = get_block_by_ts(self.chains.date_range[1], self)
        logger.info(f"set blocks for {self.name}: {start} - {end}")
        return (start, end)

    def set_pool_fee_data(self):
        """
        sets the `pool_fee_data` for the chain
        fetches from subgraph if not cached
        """
        if self.chains.use_cache and self._cache_file_path().exists():
            pool_data = self._load_core_pools_from_cache()
        else:
            pool_data = self._fetch_and_process_pool_fee_data()
            self._save_core_pools_to_cache(pool_data)

        self.pool_fee_data = pool_data

    def _cache_file_path(self) -> Path:
        """
        a cache file is uniquely identified by the chain name, start and end timestamp
        """
        filename = f"{self.name}_{self.chains.date_range[0]}_{self.chains.date_range[1]}.joblib"
        return self.chains.cache_dir / filename

    def _load_core_pools_from_cache(self) -> list[PoolFeeData]:
        logger.info(f"loading core pools from cache for {self.name}")
        return joblib.load(self._cache_file_path())

    def _save_core_pools_to_cache(self, pool_data: list[PoolFeeData]) -> None:
        joblib.dump(pool_data, self._cache_file_path())

    def _fetch_and_process_pool_fee_data(self) -> list[PoolFeeData]:
        """
        fetches various chain data from subgraph and returns a list of `PoolFeeData` based on the core pool list
        """
        logger.info(f"getting snapshots for {self.name}")

        start_snaps = self.subgraph.get_balancer_pool_snapshots(
            block=self.block_range[0], pools_per_req=1000, limit=5000
        )
        end_snaps = self.subgraph.get_balancer_pool_snapshots(
            block=self.block_range[1], pools_per_req=1000, limit=5000
        )

        pools = self.subgraph.fetch_all_pools_info()
        pool_to_gauge = self._create_pool_to_gauge_mapping(pools)

        pools_data = []

        for pool_id, label in self.bal_pools_gauges.core_pools:
            start_snap = self._get_latest_snapshot(start_snaps, pool_id)
            end_snap = self._get_latest_snapshot(end_snaps, pool_id)
            if self._should_add_pool(pool_id, start_snap, end_snap):
                pool_fee_data = self._fetch_twap_prices_and_init_pool_fee_data(pool_id, label, pool_to_gauge, start_snap, end_snap)
                pools_data.append(pool_fee_data)

        return pools_data

    def _create_pool_to_gauge_mapping(self, pools: list[Pool]) -> Dict[str, str]:
        """
        create a mapping of pool id to gauge address from the vebal_get_voting_list query
        """
        pool_to_gauge = {}
        for pool in pools:
            if pool.gauge.isKilled:
                logger.info(
                    f"gauge {pool.gauge.address} (pool id: {pool.id}) is killed, skipping"
                )
                continue
            pool_to_gauge[pool.id] = Web3.to_checksum_address(pool.gauge.address)
        return pool_to_gauge

    def _should_add_pool(
        self, pool_id: str, start_snap: PoolSnapshot, end_snap: PoolSnapshot
    ) -> bool:
        return (
            start_snap and end_snap
            and self.bal_pools_gauges.has_alive_preferential_gauge(pool_id)
        )

    def _fetch_twap_prices_and_init_pool_fee_data(
        self,
        pool_id: str,
        label: str,
        pool_to_gauge: Dict[str, str],
        start_snap: PoolSnapshot,
        end_snap: PoolSnapshot,
    ) -> PoolFeeData:
        logger.info(f"fetching twap prices for {label} on {self.name}")
        prices = self.subgraph.get_twap_price_pool(
            pool_id,
            self.name,
            self.chains.date_range,
        )

        return PoolFeeData(
            pool_id=pool_id,
            address=prices.bpt_price.address,
            symbol=label,
            bpt_price=prices.bpt_price.twap_price,
            tokens_price=prices.token_prices,
            gauge_address=pool_to_gauge[pool_id],
            start_pool_snapshot=start_snap,
            end_pool_snapshot=end_snap,
            last_join_exit_ts=self.bal_pools_gauges.get_last_join_exit(pool_id),
        )

    @staticmethod
    def _get_latest_snapshot(
        snapshots: list[PoolSnapshot], pool_id: str
    ) -> Union[PoolSnapshot, None]:
        """
        get the latest snapshot for a pool from the list of snapshots given from `pool_snapshots` query
        """
        return next(
            (
                snap
                for snap in sorted(snapshots, key=lambda x: x.timestamp, reverse=True)
                if snap.id == pool_id
            ),
            None,
        )

    @property
    def total_earned_fees_usd_twap(self) -> Decimal:
        return sum(
            [pool_data.total_earned_fees_usd_twap for pool_data in self.pool_fee_data]
        )
