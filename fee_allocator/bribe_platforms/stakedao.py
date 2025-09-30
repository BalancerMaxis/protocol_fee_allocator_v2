from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from web3 import Web3
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
from pathlib import Path
from fee_allocator.logger import logger


class StakeDAOPlatform(BribePlatform):
    """StakeDAO VoteMarket v2 platform implementation for Balancer bribes"""

    def __init__(self, book: Dict[str, str], run_config: Any):
        super().__init__(book, run_config)
        self.vote_market_address = book["stake_dao/votemarket_v1"]
        self.usdc_address = book["tokens/USDC"]

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        """
        Process StakeDAO VoteMarket bribes for Balancer market only

        Note: StakeDAO only supports Balancer market, not Aura.
        The factory should ensure only Balancer bribes are sent here.
        """
        balancer_bribes = bribes_df[bribes_df["platform"] == "balancer"]

        if balancer_bribes.empty or balancer_bribes["amount"].sum() == 0:
            logger.info("No Balancer bribes to process for StakeDAO")
            return

        base_dir = Path(__file__).parent.parent

        vote_market = SafeContract(
            self.vote_market_address,
            abi_file_path=f"{base_dir}/abi/stakedao_market.json"
        )

        total_usdc = self.get_total_approval_amount(balancer_bribes)
        if total_usdc > 0:
            usdc.approve(self.vote_market_address, total_usdc)
            logger.info(f"Approved {total_usdc / 1e6} USDC for StakeDAO VoteMarket")

        for _, row in balancer_bribes.iterrows():
            if int(row["amount"]) == 0:
                continue

            gauge_address = Web3.to_checksum_address(row["target"])
            mantissa = round(row["amount"] * 1e6)

            vote_market.createBounty(
                gauge_address,
                builder.safe_address,
                self.usdc_address,
                2,
                mantissa,
                mantissa,
                [],
                False
            )

            logger.info(f"Created StakeDAO bribe for gauge {gauge_address}: ${row['amount']:.2f}")

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """Calculate total USDC that needs approval for StakeDAO"""
        if bribes_df.empty:
            return 0
        balancer_bribes = bribes_df[bribes_df["platform"] == "balancer"]
        return int(balancer_bribes["amount"].sum() * 1e6)

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