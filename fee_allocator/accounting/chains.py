from __future__ import annotations
from typing import List, Dict, Union, Optional
from decimal import Decimal
from pathlib import Path
import os
import time
from dotenv import load_dotenv

from web3 import Web3
import requests
from bal_tools import Subgraph, BalPoolsGauges, Web3RpcByChain
import joblib
from bal_tools.subgraph import DateRange
from bal_tools.models import PoolSnapshot, Pool
from bal_tools.errors import NoResultError, NoPricesFoundError
from bal_addresses import AddrBook

from fee_allocator.accounting.core_pools import PoolFee, PoolFeeData
from fee_allocator.accounting.interfaces import AbstractCorePoolChain
from fee_allocator.accounting.models import (
    GlobalFeeConfig,
    InputFees,
    AllianceConfig,
    AlliancePool,
    PartnerPool
)
from fee_allocator.constants import (
    FEE_CONSTANTS_URL,
    ALLIANCE_CONFIG_URL
)
from fee_allocator.accounting.decorators import round, require_pool_fee_data
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
        core_pools (Dict[str, Dict[str, str]], optional): A dictionary of core pools for each chain. Defaults to None.
    """
    def __init__(
        self,
        input_fees: InputFees,
        date_range: DateRange,
        cache_dir: Path = None,
        use_cache: bool = True,
        core_pools: Dict[str, Dict[str, str]] = None,
        protocol_version: str = "v2",
    ):
        # convert wei fees to usd. identified by the lack of a decimal point
        self.input_fees = {chain: fee / 1e6 if isinstance(fee, int) else fee for chain, fee in input_fees.items()}
        self.date_range = date_range
        self.w3_by_chain = Web3RpcByChain(os.environ["DRPC_KEY"])
        self.core_pools = core_pools

        self.fee_config = GlobalFeeConfig(**requests.get(FEE_CONSTANTS_URL).json())
        
        # Load alliance config from URL
        self.alliance_config = AllianceConfig(**requests.get(ALLIANCE_CONFIG_URL).json())

        # caches a list of `PoolFeeData` for each chain
        self.use_cache = use_cache
        self.cache_dir = cache_dir if cache_dir else Path(__file__).parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)

        self._chains: Union[dict[str, CorePoolChain], None] = None
        self.aura_vebal_share: Union[Decimal, None] = None
        self.protocol_version = protocol_version


    def __getattr__(self, name):
        try:
            return self._chains[name]
        except KeyError:
            return getattr(self, name)

    def set_core_pool_chains_data(self):
        """
        iterate over each chain in `input_fees` and fetch that chain's core pool data
        chains with no core pools will have their fees allocated to dao/vebal only
        """
        _chains: dict[str, CorePoolChain] = {}

        for chain_name, fees in self.input_fees.items():
            if fees == 0:
                print(f"{chain_name} has no fees on {self.protocol_version}, skipping...")
                continue
                
            try:
                chain = CorePoolChain(self, chain_name, fees, self.w3_by_chain[chain_name])
            except ConnectionError as e:
                logger.error(f"Failed to initialize chain {chain_name} with fees {fees}. trying again after 10s...")
                # retry with delay
                time.sleep(10)
                chain = CorePoolChain(self, chain_name, fees, self.w3_by_chain[chain_name])
            chain.set_pool_fee_data()
            
            if chain.pool_fee_data:
                _chains[chain_name] = chain
            else:
                print(f"no core pools for {chain_name}")
                chain.pool_fee_data = []
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
    def total_to_beets_usd(self) -> Decimal:
        return sum(
            [pool.to_beets_usd for chain in self.all_chains for pool in chain.core_pools]
        )

    @property
    @round(4)
    def total_to_incentives_usd(self) -> Decimal:
        return sum(
            [
                pool.total_to_incentives_usd + pool.to_dao_usd + pool.to_vebal_usd + pool.to_beets_usd
                for chain in self.all_chains
                for pool in chain.core_pools
            ]
        )

    @property
    @round(4)
    def total_fees_collected_usd(self) -> Decimal:
        return sum(chain.fees_collected for chain in self.all_chains)

    @property
    @round(4)
    def total_to_vebal_usd(self) -> Decimal:
        return sum(chain.total_to_vebal_usd for chain in self.all_chains)


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
        self.core_pools_list = self.chains.core_pools.get(self.name, {}) if self.chains.core_pools else None
        self.alliance_pools: List[AlliancePool] = []

        try:
            self.chain_id = AddrBook.chain_ids_by_name[self.name]
        except KeyError:
            raise ValueError(f"chain id for {self.name} not found in `AddrBook`")

        self.fees_collected = Decimal(self.fees_collected)
        self.subgraph = Subgraph(self.name)
        self.bal_pools_gauges = BalPoolsGauges(self.name, use_cached_core_pools=True)

        self.block_range = self._set_block_range()
        self.pool_fee_data: Union[list[PoolFeeData], None] = None
        self.core_pools: List[PoolFee] = []
        self.alliance_noncore_fee_data: List[PoolFeeData] = []
        self.partner_pools: List[PartnerPool] = []
        self.partner_noncore_fee_data: List[PoolFeeData] = []

    def _set_block_range(self) -> tuple[int, int]:
        start = get_block_by_ts(self.chains.date_range[0], self)
        end = get_block_by_ts(self.chains.date_range[1], self)
        logger.info(f"set blocks for {self.name}: {start} - {end}")
        return (start, end)
    
    def _init_alliance_pools(self) -> None:
        """
        Initialize alliance pools for the current chain.
        Uses TVL thresholds from alliance config to determine eligibility.
        """
        all_alliance_pools = [
            pool
            for member in self.chains.alliance_config.alliance_members
            for pool in member.pools
            if pool.network == self.name and pool.active
        ]
        
        self.alliance_pools = []
        thresholds = self.chains.alliance_config.alliance_thresholds

        allocator_version = int(self.chains.protocol_version.replace("v", ""))

        for pool in all_alliance_pools:
            protocol_version = self.subgraph.get_pool_protocol_version(pool.pool_id)
            
            if protocol_version not in [2, 3]:
                logger.warning(f"Alliance pool {pool.pool_id} has unknown protocol version {protocol_version}")
                continue

            if protocol_version != allocator_version:
                continue

            tvl_threshold = thresholds.v2_min_tvl if protocol_version == 2 else thresholds.v3_min_tvl
            
            if tvl_threshold == 0:
                self.alliance_pools.append(pool)
                logger.info(f"v{protocol_version} Alliance pool: {pool.pool_id} added as partner pool")
                continue
            
            try:
                tvl = self.bal_pools_gauges.get_pool_tvl(pool.pool_id)
                if tvl >= tvl_threshold:
                    self.alliance_pools.append(pool)
                    logger.info(f"v{protocol_version} Alliance pool: {pool.pool_id} added as partner pool")
                else:
                    logger.info(f"v{protocol_version} Alliance pool: {pool.pool_id} skipped - TVL ${tvl:,.2f} below ${tvl_threshold:,.2f} threshold")
            except Exception as e:
                logger.error(f"Failed to get TVL for v{protocol_version} Alliance pool {pool.pool_id}: {e}")
    
    def set_partner_pools(self):
        if not self.chains.alliance_config.partners:
            return
        
        allocator_version = int(self.chains.protocol_version.replace("v", ""))
        
        for partner in self.chains.alliance_config.partners:
            if not partner.active:
                continue
                
            for pool in partner.pools:
                if not pool.active:
                    continue
                    
                if pool.network != self.name:
                    continue
                
                protocol_version = self.subgraph.get_pool_protocol_version(pool.pool_id)
                
                if protocol_version not in [2, 3]:
                    logger.warning(f"Partner pool {pool.pool_id} has unknown protocol version {protocol_version}")
                    continue
                
                if protocol_version != allocator_version:
                    continue
                
                self.partner_pools.append(pool)
                logger.info(f"v{protocol_version} Partner pool: {pool.pool_id} from {partner.name} added (no criteria applied)")

    def set_pool_fee_data(self):
        """
        sets list of `PoolFeeData` for the chain
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
        a cache file is uniquely identified by the chain name, protocol version, and timestamps
        """
        filename = f"{self.name}_{self.chains.protocol_version}_{self.chains.date_range[0]}_{self.chains.date_range[1]}.joblib"
        return self.chains.cache_dir / filename

    def _load_core_pools_from_cache(self) -> list[PoolFeeData]:
        cached_data = joblib.load(self._cache_file_path())
        self.alliance_pools = cached_data.get('alliance_pools', [])
        self.alliance_noncore_fee_data = cached_data.get('alliance_noncore_fee_data', [])
        self.partner_pools = cached_data.get('partner_pools', [])
        self.partner_noncore_fee_data = cached_data.get('partner_noncore_fee_data', [])
        return cached_data.get('pool_fee_data', [])

    def _save_core_pools_to_cache(self, pool_data: list[PoolFeeData]) -> None:
        cache_data = {
            'pool_fee_data': pool_data,
            'alliance_pools': self.alliance_pools,
            'alliance_noncore_fee_data': self.alliance_noncore_fee_data,
            'partner_pools': self.partner_pools,
            'partner_noncore_fee_data': self.partner_noncore_fee_data
        }
        joblib.dump(cache_data, self._cache_file_path())

    def _fetch_and_process_pool_fee_data(self) -> list[PoolFeeData]:
        """
        fetches various chain data from subgraph and returns a list of `PoolFeeData` based on the core pool list
        """
        self._init_alliance_pools()
        self.set_partner_pools()

        if not self.bal_pools_gauges.core_pools and not self.alliance_pools and not self.partner_pools:
            return []

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

        core_pools_list = list(
            [(pool_id, label) for pool_id, label in self.core_pools_list.items()]
            if self.core_pools_list is not None
            else self.bal_pools_gauges.core_pools
        )

        alliance_pool_tuples = [(pool.pool_id, pool.partner) for pool in self.alliance_pools]
        
        # Get partner names for partner pools
        partner_pool_tuples = []
        for partner in self.chains.alliance_config.partners or []:
            for pool in partner.pools:
                # Check if this partner pool is in our active partner pools list
                if any(p.pool_id == pool.pool_id for p in self.partner_pools):
                    partner_pool_tuples.append((pool.pool_id, f"Partner:{partner.name}"))

        core_pools_dict = dict(core_pools_list)
        core_pools_dict.update(dict(alliance_pool_tuples))
        core_pools_dict.update(dict(partner_pool_tuples))
        core_pools_list = list(core_pools_dict.items())

        for pool_id, label in core_pools_list:
            protocol_version = self.subgraph.get_pool_protocol_version(pool_id)

            if protocol_version == 3 and self.chains.protocol_version == "v3":
                pool_fee_data = self._fetch_twap_prices_and_init_pool_fee_data_v3(pool_id, label, pool_to_gauge)
                alliance_pool = next((p for p in self.alliance_pools if p.pool_id == pool_id), None)
                partner_pool = next((p for p in self.partner_pools if p.pool_id == pool_id), None)
                
                # Check if this is a non-core pool (either alliance or partner)
                is_non_core = False
                if alliance_pool and alliance_pool.pool_type != "core":
                    self.alliance_noncore_fee_data.append(pool_fee_data)
                    is_non_core = True
                elif partner_pool:
                    # Partner pools can be in core pools list or not
                    # If it's not in the original core pools list, it's non-core
                    original_core_pool_ids = [pool_id for pool_id, _ in self.bal_pools_gauges.core_pools]
                    if pool_id not in original_core_pool_ids:
                        self.partner_noncore_fee_data.append(pool_fee_data)
                        is_non_core = True
                
                if not is_non_core:
                    pools_data.append(pool_fee_data)

            elif protocol_version == 2 and self.chains.protocol_version == "v2":
                start_snap = self._get_latest_snapshot(start_snaps, pool_id)
                end_snap = self._get_latest_snapshot(end_snaps, pool_id)
                alliance_pool = next((p for p in self.alliance_pools if p.pool_id == pool_id), None)
                partner_pool = next((p for p in self.partner_pools if p.pool_id == pool_id), None)
                
                # Partner pools bypass _should_add_pool check
                if alliance_pool or partner_pool or self._should_add_pool(pool_id, start_snap, end_snap, pool_to_gauge):
                    pool_fee_data = self._fetch_twap_prices_and_init_pool_fee_data_v2(pool_id, label, pool_to_gauge, start_snap, end_snap)
                    
                    # Check if this is a non-core pool (either alliance or partner)
                    is_non_core = False
                    if alliance_pool and alliance_pool.pool_type != "core":
                        self.alliance_noncore_fee_data.append(pool_fee_data)
                        is_non_core = True
                    elif partner_pool:
                        # Partner pools can be in core pools list or not
                        # If it's not in the original core pools list, it's non-core
                        original_core_pool_ids = [pool_id for pool_id, _ in self.bal_pools_gauges.core_pools]
                        if pool_id not in original_core_pool_ids:
                            self.partner_noncore_fee_data.append(pool_fee_data)
                            is_non_core = True
                    
                    if not is_non_core:
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
        self, pool_id: str, start_snap: PoolSnapshot, end_snap: PoolSnapshot, pool_to_gauge: Dict[str, str]
    ) -> bool:
        return (
            start_snap and end_snap
            and self.bal_pools_gauges.has_alive_preferential_gauge(pool_id)
            and pool_to_gauge.get(pool_id)
        )

    def _fetch_twap_prices_and_init_pool_fee_data_v2(
        self,
        pool_id: str,
        label: str,
        pool_to_gauge: Dict[str, str],
        start_snap: PoolSnapshot,
        end_snap: PoolSnapshot,
    ) -> Optional[PoolFeeData]:
        logger.info(f"fetching twap prices for {label} on {self.name}")
        try:
            prices = self.subgraph.get_twap_price_pool(
                pool_id,
                self.name,
                self.chains.date_range,
            )
        except NoPricesFoundError:
            return None
        try:
            last_join_exit_ts = self.bal_pools_gauges.get_last_join_exit(pool_id)
        except NoResultError:
            last_join_exit_ts = 0

        return PoolFeeData(
            pool_id=pool_id,
            address=prices.bpt_price.address,
            symbol=label,
            bpt_price=prices.bpt_price.twap_price,
            tokens_price=prices.token_prices,
            gauge_address=pool_to_gauge.get(pool_id),
            start_pool_snapshot=start_snap,
            end_pool_snapshot=end_snap,
            last_join_exit_ts=last_join_exit_ts,
            protocol_version=2,
        )
       
    def _fetch_twap_prices_and_init_pool_fee_data_v3(
        self,
        pool_id: str,
        label: str,
        pool_to_gauge: Dict[str, str],
    ) -> Optional[PoolFeeData]:
        logger.info(f"fetching twap prices for {label} on {self.name}")

        try:
            last_join_exit_ts = self.bal_pools_gauges.get_last_join_exit(pool_id)
        except NoResultError:
            last_join_exit_ts = 0

        try:
            return PoolFeeData(
                pool_id=pool_id,
                address=pool_id,
                symbol=label,
                tokens_price=None,
                gauge_address=pool_to_gauge.get(pool_id),
                start_pool_snapshot=None,
                end_pool_snapshot=None,
                last_join_exit_ts=last_join_exit_ts,
                protocol_version=3,
                total_earned_fees_usd_twap=self.subgraph.get_v3_protocol_fees(pool_id, self.name, self.chains.date_range),
            )
        except NoPricesFoundError:
            return None
        
    def get_alliance_noncore_partner_fee(self, pool_id: str) -> Decimal:
        noncore_pool = next((p for p in self.alliance_noncore_fee_data if p.pool_id == pool_id), None)
        if not noncore_pool or self.alliance_noncore_fees_collected == 0:
            return Decimal(0)
            
        return (
            noncore_pool.total_earned_fees_usd_twap / self.alliance_noncore_fees_collected
            * self.alliance_noncore_fees_collected
            * self.chains.alliance_config.alliance_fee_allocations["non_core"].partner_share_pct
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
    @require_pool_fee_data
    def total_earned_fees_usd_twap(self) -> Decimal:
        return sum(
            [pool_data.total_earned_fees_usd_twap for pool_data in self.pool_fee_data]
        )

    @property
    @require_pool_fee_data
    def noncore_fees_collected(self) -> Decimal:
        return max(self.fees_collected - self.total_earned_fees_usd_twap - self.alliance_noncore_fees_collected, Decimal(0))

    @property
    @require_pool_fee_data
    def noncore_to_dao_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        return self.noncore_fees_collected * (1 - beets_share_pct) * self.chains.fee_config.noncore_dao_share_pct

    @property
    @require_pool_fee_data
    def noncore_to_vebal_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        return self.noncore_fees_collected * (1 - beets_share_pct) * self.chains.fee_config.noncore_vebal_share_pct
    
    @property
    @require_pool_fee_data
    def noncore_to_beets_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        if beets_share_pct == 0:
            return Decimal(0)
        return self.noncore_fees_collected * beets_share_pct

    @property
    @require_pool_fee_data
    def total_fees_earned(self) -> Decimal:
        return self.total_earned_fees_usd_twap + self.noncore_fees_collected + self.alliance_noncore_fees_collected
    
    @property
    def alliance_noncore_fees_collected(self) -> Decimal:
        return sum(pool.total_earned_fees_usd_twap for pool in self.alliance_noncore_fee_data)

    @property
    def alliance_noncore_to_dao_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        return self.alliance_noncore_fees_collected * self.chains.alliance_config.alliance_fee_allocations["non_core"].dao_share_pct * (1 - beets_share_pct)

    @property
    def alliance_noncore_to_vebal_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        return self.alliance_noncore_fees_collected * self.chains.alliance_config.alliance_fee_allocations["non_core"].vebal_share_pct  * (1 - beets_share_pct)

    @property
    def alliance_noncore_to_beets_usd(self) -> Decimal:
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        if beets_share_pct == 0:
            return Decimal(0)
        return self.alliance_noncore_fees_collected * (1 - self.chains.alliance_config.alliance_fee_allocations["non_core"].partner_share_pct) * beets_share_pct
    
    @property
    def partner_noncore_fees_collected(self) -> Decimal:
        return sum(pool.total_earned_fees_usd_twap for pool in self.partner_noncore_fee_data)
    
    def get_partner_noncore_fee(self, pool_id: str) -> Decimal:
        """Get partner fee for a specific non-core partner pool"""
        noncore_pool = next((p for p in self.partner_noncore_fee_data if p.pool_id == pool_id), None)
        if not noncore_pool or self.partner_noncore_fees_collected == 0:
            return Decimal(0)
        
        # Find the partner and fee config for this pool
        partner_info = self.chains.alliance_config.get_partner_pool_config(pool_id, self.name, is_core=False)
        if not partner_info:
            return Decimal(0)
            
        _, fee_config = partner_info
        return (
            noncore_pool.total_earned_fees_usd_twap / self.partner_noncore_fees_collected
            * self.partner_noncore_fees_collected
            * fee_config.partner_share_pct
        )
    
    @property
    def partner_noncore_to_dao_usd(self) -> Decimal:
        """DAO fees from partner non-core pools"""
        total = Decimal(0)
        for pool in self.partner_noncore_fee_data:
            partner_info = self.chains.alliance_config.get_partner_pool_config(pool.pool_id, self.name, is_core=False)
            if partner_info:
                _, fee_config = partner_info
                pool_share = pool.total_earned_fees_usd_twap / self.partner_noncore_fees_collected if self.partner_noncore_fees_collected > 0 else Decimal(0)
                beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
                total += self.partner_noncore_fees_collected * pool_share * fee_config.dao_share_pct * (1 - beets_share_pct)
        return total
    
    @property
    def partner_noncore_to_vebal_usd(self) -> Decimal:
        """veBAL fees from partner non-core pools"""
        total = Decimal(0)
        for pool in self.partner_noncore_fee_data:
            partner_info = self.chains.alliance_config.get_partner_pool_config(pool.pool_id, self.name, is_core=False)
            if partner_info:
                _, fee_config = partner_info
                pool_share = pool.total_earned_fees_usd_twap / self.partner_noncore_fees_collected if self.partner_noncore_fees_collected > 0 else Decimal(0)
                beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
                total += self.partner_noncore_fees_collected * pool_share * fee_config.vebal_share_pct * (1 - beets_share_pct)
        return total
    
    @property
    def partner_noncore_to_beets_usd(self) -> Decimal:
        """Beets fees from partner non-core pools (Optimism only)"""
        beets_share_pct = self.chains.fee_config.beets_share_pct if self.name == "optimism" else 0
        if beets_share_pct == 0:
            return Decimal(0)
        
        total = Decimal(0)
        for pool in self.partner_noncore_fee_data:
            partner_info = self.chains.alliance_config.get_partner_pool_config(pool.pool_id, self.name, is_core=False)
            if partner_info:
                _, fee_config = partner_info
                pool_share = pool.total_earned_fees_usd_twap / self.partner_noncore_fees_collected if self.partner_noncore_fees_collected > 0 else Decimal(0)
                # Beets gets a share of the non-partner portion
                total += self.partner_noncore_fees_collected * pool_share * (1 - fee_config.partner_share_pct) * beets_share_pct
        return total

    @property
    @round(4)
    def total_to_vebal_usd(self) -> Decimal:
        core_vebal = sum(pool.to_vebal_usd for pool in self.core_pools)
        return core_vebal +  self.noncore_to_vebal_usd + self.alliance_noncore_to_vebal_usd + self.partner_noncore_to_vebal_usd
