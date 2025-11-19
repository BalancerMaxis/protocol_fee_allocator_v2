from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from web3 import Web3
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
from fee_allocator.utils import get_hh_aura_target
from pathlib import Path


class HiddenHandPlatform(BribePlatform):
    """HiddenHand bribe platform implementation"""

    def __init__(self, book: Dict[str, str], run_config: Any):
        super().__init__(book, run_config)
        self.bal_briber = book["hidden_hand2/balancer_briber"]
        self.aura_briber = book["hidden_hand2/aura_briber"]
        self.bribe_vault = book["hidden_hand2/bribe_vault"]

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        """Process HiddenHand bribes for both Balancer and Aura markets"""

        if bribes_df.empty or bribes_df["amount"].sum() == 0:
            return

        base_dir = Path(__file__).parent.parent

        bal_bribe_market = SafeContract(
            self.bal_briber,
            abi_file_path=f"{base_dir}/abi/bribe_market.json"
        )
        aura_bribe_market = SafeContract(
            self.aura_briber,
            abi_file_path=f"{base_dir}/abi/bribe_market.json"
        )

        total_usdc = self.get_total_approval_amount(bribes_df)
        if total_usdc > 0:
            usdc.approve(self.bribe_vault, total_usdc + 1)

        for _, row in bribes_df.iterrows():
            if int(row["amount"]) == 0:
                continue

            prop_hash = self._get_prop_hash(row["platform"], row["target"])
            mantissa = round(row["amount"] * 1e6)

            if row["platform"] == "balancer":
                bal_bribe_market.depositBribe(
                    prop_hash,
                    self.book["tokens/USDC"],
                    mantissa,
                    0,
                    2
                )
            elif row["platform"] == "aura":
                aura_bribe_market.depositBribe(
                    prop_hash,
                    self.book["tokens/USDC"],
                    mantissa,
                    0,
                    1
                )

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """Calculate total USDC that needs approval"""
        if bribes_df.empty:
            return 0
        return int(bribes_df["amount"].sum() * 1e6)

    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """HiddenHand doesn't have specific gauge requirements"""
        return True, None

    @property
    def platform_name(self) -> str:
        """Platform identifier for reporting"""
        return "hh"

    @property
    def supported_markets(self) -> List[str]:
        return ["aura", "balancer"]

    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        return "hh"

    @staticmethod
    def _get_prop_hash(platform: str, target: str) -> str:
        """Generate proposal hash for HiddenHand"""
        if platform == "balancer":
            prop = Web3.solidity_keccak(["address"], [Web3.to_checksum_address(target)])
            return f"0x{prop.hex().replace('0x', '')}"
        if platform == "aura":
            return get_hh_aura_target(target)
        raise ValueError(f"platform {platform} not supported")