from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
import json
from pathlib import Path


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
            fee_amount = (total_reward_amount * fee_ratio) // 10000

            reward_per_period = total_reward_amount // 2
            max_reward_per_vote = max(reward_per_period // 1000, 50)
            min_reward_per_vote = 50

            quest_board.createRangedQuest(
                row["target"],
                self.usdc_address,
                "true",
                2,
                min_reward_per_vote,
                max_reward_per_vote,
                total_reward_amount,
                fee_amount,
                0,
                1,
                "[]"
            )


    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """No validation needed for ROLLOVER close type"""
        return True, None

    @property
    def platform_name(self) -> str:
        """Platform identifier for reporting"""
        return "paladin"

    @property
    def supported_markets(self) -> List[str]:
        return ["aura", "balancer"]

    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        return "paladin"