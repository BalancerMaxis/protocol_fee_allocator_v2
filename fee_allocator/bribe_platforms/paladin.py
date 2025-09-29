from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from web3 import Web3
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
import json
from pathlib import Path
from fee_allocator.logger import logger


class PaladinPlatform(BribePlatform):
    """Paladin Quest platform implementation"""

    def __init__(self, book: Dict[str, str], run_config: Any):
        super().__init__(book, run_config)
        self.bal_quest_board = book["paladin/QuestBoardV2_1"]
        self.aura_quest_board = book["paladin/QuestBoardV2_1Aura"]
        self.usdc_address = book["tokens/USDC"]

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        """Process Paladin Quest bribes for both Balancer and Aura markets"""

        valid_bribes = bribes_df[bribes_df["amount"] > 0]
        if valid_bribes.empty:
            return

        base_dir = Path(__file__).parent.parent

        with open(f"{base_dir}/abi/paladin_quest_board.json", "r") as f:
            paladin_abi = json.load(f)

        quest_boards = {}
        platform_fee_ratios = {}

        for platform in ["balancer", "aura"]:
            bribes = valid_bribes[valid_bribes["platform"] == platform]
            if bribes.empty:
                continue

            quest_board_addr = self.bal_quest_board if platform == "balancer" else self.aura_quest_board
            quest_boards[platform] = SafeContract(quest_board_addr, abi=paladin_abi)

            w3_contract = self.run_config.mainnet.web3.eth.contract(
                address=quest_board_addr,
                abi=paladin_abi
            )
            try:
                platform_fee_ratios[platform] = w3_contract.functions.platformFeeRatio().call()
            except Exception:
                platform_fee_ratios[platform] = 400

            total = sum(round(row["amount"] * 1e6) for _, row in bribes.iterrows())
            if total > 0:
                usdc.approve(quest_board_addr, total)

        for _, row in valid_bribes.iterrows():
            mantissa = round(row["amount"] * 1e6)
            platform = row["platform"]
            quest_board = quest_boards[platform]
            fee_ratio = platform_fee_ratios[platform]

            total_reward_amount = int(mantissa * 10000 / (10000 + fee_ratio))
            fee_amount = mantissa - total_reward_amount

            quest_board.createRangedQuest(
                row["target"],
                self.usdc_address,
                True,
                2,
                1,
                total_reward_amount,
                total_reward_amount,
                fee_amount,
                0,
                1,
                []
            )

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """Calculate total USDC that needs approval for each quest board"""
        return 0

    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """Validate gauge has USDC as reward token with correct distributor"""
        try:
            base_dir = Path(__file__).parent.parent
            with open(f"{base_dir}/abi/gauge.json", "r") as f:
                gauge_abi = json.load(f)

            w3 = self.run_config.mainnet.web3
            usdc = Web3.to_checksum_address(self.usdc_address)
            gauge = Web3.to_checksum_address(gauge_address)
            contract = w3.eth.contract(address=gauge, abi=gauge_abi)

            try:
                usdc_found = usdc in [contract.functions.reward_tokens(i).call() for i in range(8)]
            except Exception:
                return False, "Gauge has incompatible implementation"

            if not usdc_found:
                return False, f"USDC ({usdc}) not found in gauge reward tokens"

            try:
                distributor = contract.functions.reward_data(usdc).call()[1]
                has_correct_distributor = (
                    distributor.lower() == self.bal_quest_board.lower() or
                    distributor.lower() == self.aura_quest_board.lower()
                )

                if not has_correct_distributor:
                    valid_distributors = [
                        f"Balancer: {self.bal_quest_board}",
                        f"Aura: {self.aura_quest_board}"
                    ]
                    return False, f"Incorrect distributor. Valid: {', '.join(valid_distributors)}"

            except Exception:
                return False, "Could not verify distributor"

            return True, None

        except Exception as e:
            return False, f"Validation error: {str(e)}"

    @property
    def platform_name(self) -> str:
        """Platform identifier for reporting"""
        return "paladin"

    @property
    def supported_markets(self) -> List[str]:
        return ["aura", "balancer"]

    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        return "paladin"

    def check_all_gauge_requirements(self, pools: List[Any]) -> List[Dict]:
        """Check all Paladin gauges for requirements and return issues"""
        gauges_with_issues = []

        for pool in pools:
            if pool.market_override != "paladin":
                continue

            valid, error_msg = self.validate_gauge_requirements(pool.gauge_address)
            if not valid:
                action_needed = []

                # Determine which distributors are needed
                if pool.to_bal_incentives_usd > 0:
                    action_needed.append(f"Balancer distributor ({self.bal_quest_board})")
                if pool.to_aura_incentives_usd > 0:
                    action_needed.append(f"Aura distributor ({self.aura_quest_board})")

                if action_needed:
                    if "not found in gauge reward tokens" in error_msg:
                        action_msg = f"Add USDC ({self.usdc_address}) as reward token and set {' and '.join(action_needed)}"
                    elif "Incorrect distributor" in error_msg:
                        action_msg = f"Set {' and '.join(action_needed)}"
                    else:
                        action_msg = error_msg

                    logger.warning(f"Paladin gauge {pool.gauge_address} missing requirements: {action_msg}")
                    gauges_with_issues.append({
                        "gauge": pool.gauge_address,
                        "pool_id": pool.pool_id,
                        "chain": pool.chain.name,
                        "action": action_msg,
                        "amount": float(pool.total_to_incentives_usd)
                    })

        return gauges_with_issues