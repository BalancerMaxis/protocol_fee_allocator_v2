from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from web3 import Web3
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
from pathlib import Path
from fee_allocator.logger import logger
from bal_addresses import AddrBook


class StakeDAOPlatform(BribePlatform):
    """StakeDAO VoteMarket v2 platform implementation for Balancer bribes"""

    SUPPORTED_L2_CHAINS = ["arbitrum", "optimism", "base", "polygon"]

    def __init__(self, book: Dict[str, str], run_config: Any):
        super().__init__(book, run_config)
        self.campaign_remote_manager_address = "0x53aD4Cd1F1e52DD02aa9FC4A8250A1b74F351CA2"
        self.vote_market_v2_address = "0xDD2FaD5606cD8ec0c3b93Eb4F9849572b598F4c7" # same on all chains (doesn't exist on mainnet)
        self.usdc_address = book["tokens/USDC"]
        self._gauge_to_chain_cache = {}
        self._build_gauge_to_chain_map()

    def _build_gauge_to_chain_map(self):
        """Build a mapping of gauge addresses to their chains from the run config."""
        if not self.run_config or not hasattr(self.run_config, 'all_chains'):
            return

        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                if pool.gauge_address:
                    self._gauge_to_chain_cache[pool.gauge_address.lower()] = chain.name

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        balancer_bribes = bribes_df[bribes_df["platform"] == "balancer"]

        if balancer_bribes.empty or balancer_bribes["amount"].sum() == 0:
            logger.info("No Balancer bribes to process for StakeDAO")
            return

        base_dir = Path(__file__).parent.parent
        campaign_manager = SafeContract(
            self.campaign_remote_manager_address,
            abi_file_path=f"{base_dir}/abi/stakedao_marketv2.json"
        )

        total_usdc = sum(int(row["amount"] * 1e6) for _, row in balancer_bribes.iterrows() if row["amount"] > 0)
        if total_usdc > 0:
            usdc.approve(self.campaign_remote_manager_address, total_usdc)
            logger.info(f"Approved {total_usdc / 1e6} USDC for StakeDAO CampaignRemoteManager")

        for _, row in balancer_bribes.iterrows():
            if int(row["amount"]) == 0:
                continue

            gauge_address = Web3.to_checksum_address(row["target"])
            chain_name = self._gauge_to_chain_cache.get(gauge_address.lower(), "mainnet")
            chain_id = AddrBook.chain_ids_by_name.get(chain_name)
            mantissa = round(row["amount"] * 1e6)

            # Mainnet gauges: campaigns are created on Arbitrum
            # non-mainnet gauges: campaigns are created on the same chain
            if chain_name == "mainnet":
                destination_chain_id = AddrBook.chain_ids_by_name["arbitrum"]
            elif chain_name in self.SUPPORTED_L2_CHAINS:
                destination_chain_id = chain_id
            else:
                raise ValueError(f"Chain {chain_name} not supported by StakeDAO v2. Supported chains: mainnet, {', '.join(self.SUPPORTED_L2_CHAINS)}")

            campaign_params = (
                chain_id,  # chainId (of the gauge)
                gauge_address,  # gauge
                builder.safe_address,  # manager
                self.usdc_address,  # rewardToken
                2,  # numberOfPeriods
                mantissa,  # maxRewardPerVote
                mantissa,  # totalRewardAmount
                [],  # whitelist
                "0x0000000000000000000000000000000000000000",  # hook
                False  # isWhitelist
            )

            # TODO: Calculate appropriate msg.value for CCIP fees (currently 0)
            campaign_manager.createCampaign(
                campaign_params,
                destination_chain_id,
                0,
                self.vote_market_v2_address
            )

            destination_chain_name = "arbitrum" if chain_name == "mainnet" else chain_name
            logger.info(f"Created StakeDAO v2 bribe for {chain_name} gauge {gauge_address} (campaign on {destination_chain_name}): ${row['amount']:.2f}")

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """Calculate total USDC that needs approval for StakeDAO.

        Returns 0 because approvals are handled in process_bribes method.
        """
        return 0

    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """StakeDAO doesn't have specific gauge requirements"""
        return True, None

    @property
    def platform_name(self) -> str:
        """Platform identifier for reporting"""
        return "stakedao"

    @property
    def supported_markets(self) -> List[str]:
        return ["balancer"]

    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        if market == "aura":
            return "hh"

        if market == "balancer":
            if voting_pool_override == "aura":
                return "hh"
            return "stakedao"

        return "hh"