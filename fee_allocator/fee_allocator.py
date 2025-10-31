from typing import TypedDict, Union, Dict, List
from bal_tools.subgraph import DateRange
from bal_tools.safe_tx_builder import SafeTxBuilder, SafeContract
from bal_addresses import AddrBook
from bal_tools.utils import get_abi
import pandas as pd
from decimal import Decimal
import datetime
from pathlib import Path
from web3 import Web3
from dotenv import load_dotenv
import json

from fee_allocator.accounting.chains import CorePoolChain, CorePoolRunConfig
from fee_allocator.accounting.core_pools import PoolFee
from fee_allocator.accounting import PROJECT_ROOT
from fee_allocator.utils import get_hh_aura_target
from fee_allocator.logger import logger
from fee_allocator.payload_visualizer import save_markdown_report

load_dotenv()


class InputFees(TypedDict):
    chainname: Union[int, float]


base_dir = Path(__file__).parent


class FeeAllocator:
    """
    Orchestrates the overall fee allocation workflow,
    initializes the `CorePoolRunConfig` and contains methods for redistributing fees and generating csvs/payloads.

    Args:
        input_fees (InputFees): A dictionary of chain names to fee amounts.
        date_range (DateRange): The date range for the fee allocation period.
        cache_dir (Path, optional): The directory to use for caching. Defaults to fee_allocator/cache.
        use_cache (bool, optional): Whether to use cached data. Defaults to True.
        core_pools (Dict[str, Dict[str, str]], optional): A dictionary of core pools. Defaults to None.
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
        self.input_fees = input_fees
        self.date_range = date_range
        self.start_date = datetime.datetime.fromtimestamp(date_range[0]).date()
        self.end_date = datetime.datetime.fromtimestamp(date_range[1]).date()
        self.run_config = CorePoolRunConfig(
            input_fees,
            date_range,
            cache_dir=cache_dir,
            use_cache=use_cache,
            core_pools=core_pools,
            protocol_version=protocol_version,
        )
        self.book = AddrBook("mainnet").flatbook

    def allocate(self, redistribute=True):
        """
        Allocates protocol fees to core pools and non-core pools according to BIP-734.
        Core pools: 70% voting incentives, 12.5% veBAL, 17.5% DAO
        Non-core pools: 82.5% veBAL, 17.5% DAO
        """
        self.run_config.set_core_pool_chains_data()
        self.run_config.set_aura_vebal_share()
        self.run_config.set_initial_pool_allocation()
        if redistribute:
            self.redistribute_fees()

    def redistribute_fees(self):
        """
        Redistributes fees among pools based on minimum incentive amounts and chain-specific rules.

        This method performs the following steps:
        1. Identifies pools with total incentives below the minimum threshold ($500)
        2. Redistributes fees from these pools to eligible pools above the threshold
        3. Recalculates incentive amounts for Aura and Balancer based on veBAL share
        4. Calls _handle_aura_min twice (with and without buffer) to enforce AURA minimums
        5. Calls _filter_dusty_bal_incentives to handle dust amounts and final redistribution
        """
        min_amount = self.run_config.fee_config.min_vote_incentive_amount
        
        for chain in self.run_config.all_chains:
            pools_to_redistribute = [p for p in chain.core_pools if p.total_to_incentives_usd < min_amount]
            pools_to_receive = [p for p in chain.core_pools if p.total_to_incentives_usd >= min_amount]

            if not pools_to_receive:
                continue

            total_fees_to_redistribute = sum(p.total_to_incentives_usd for p in pools_to_redistribute)
            total_weight = sum(p.total_earned_fees_usd_twap for p in pools_to_receive)

            for pool in pools_to_redistribute:
                pool.redirected_incentives_usd -= pool.total_to_incentives_usd
                pool.to_aura_incentives_usd = Decimal(0)
                pool.to_bal_incentives_usd = Decimal(0)
                pool.total_to_incentives_usd = Decimal(0)

            for pool in pools_to_receive:
                weight = pool.total_earned_fees_usd_twap / total_weight
                total = total_fees_to_redistribute * weight
                pool.total_to_incentives_usd += total
                pool.redirected_incentives_usd += total

                pool.to_aura_incentives_usd += total if pool.is_alliance_core_pool else total * self.run_config.aura_vebal_share
                pool.to_bal_incentives_usd += Decimal(0) if pool.is_alliance_core_pool else total * (1 - self.run_config.aura_vebal_share)

        self._handle_aura_min(buffer=0.25)
        self._handle_aura_min()
        self._filter_dusty_bal_incentives()
    
    def generate_artifacts(self, include_bal_transfer: bool = True) -> Dict[str, Path]:
        """
        Generates all fee allocation artifacts (CSVs and payload).
        """
        logger.info("generating fee allocation artifacts")
        
        self._check_paladin_gauge_requirements()
        
        incentives_path = self.generate_incentives_csv()
        bribe_path = self.generate_bribe_csv()
        alliance_path = self.generate_alliance_csv()
        partner_path = self.generate_partner_csv()
        noncore_path = self.generate_noncore_csv()
        
        payload_path = self.generate_bribe_payload(
            bribe_path, 
            partner_csv=partner_path,
            alliance_csv=alliance_path,
            include_bal_transfer=include_bal_transfer
        )
        
        return {
            "incentives_csv": incentives_path,
            "bribe_csv": bribe_path,
            "alliance_csv": alliance_path,
            "partner_csv": partner_path,
            "noncore_csv": noncore_path,
            "payload": payload_path
        }

    def _handle_aura_min(self, buffer=0):
        """
        Ensures all pools meet the minimum AURA incentive threshold.

        1. Identifies pools below the minimum AURA threshold (or with BAL-only overrides)
        2. Moves their AURA amounts to BAL, creating a "debt" to be redistributed
        3. Redistributes this debt from other pools' BAL to AURA proportionally
        4. Ensures donor pools maintain the minimum threshold after transfers
        5. Repeats until all pools meet the threshold or no more transfers are possible
        """
        min_aura_incentive = Decimal(self.run_config.fee_config.min_aura_incentive * (1 - buffer))
        for chain in self.run_config.all_chains:
            while True:
                debt_to_aura = Decimal(0)
                pools_below_min: List[PoolFee] = []

                for pool in chain.core_pools:
                    if pool.to_aura_incentives_usd < min_aura_incentive or (
                        pool.voting_pool_override == "bal"
                    ):
                        debt_to_aura += pool.to_aura_incentives_usd
                        pools_below_min.append(pool)

                if not debt_to_aura:
                    break

                for pool in pools_below_min:
                    pool.to_bal_incentives_usd += pool.to_aura_incentives_usd
                    pool.to_aura_incentives_usd = Decimal(0)

                pools_over_min = [
                    p
                    for p in chain.core_pools
                    if p.to_aura_incentives_usd >= min_aura_incentive and p.to_bal_incentives_usd > 0
                ]

                if not pools_over_min:
                    break

                pools_over_min.sort(key=lambda p: p.to_bal_incentives_usd, reverse=True)
                debt_remaining = debt_to_aura
                total_available_bal = sum(p.to_bal_incentives_usd for p in pools_over_min)
                
                if total_available_bal == 0:
                    break
                
                transfers_made = False
                for pool in pools_over_min:
                    if debt_remaining <= 0:
                        break
                    
                    pool_share = pool.to_bal_incentives_usd / total_available_bal
                    amount_to_transfer = min(
                        debt_remaining * pool_share,
                        pool.to_bal_incentives_usd,
                        # ensure pool stays above minimum after transfer
                        max(Decimal(0), pool.to_aura_incentives_usd + pool.to_bal_incentives_usd - min_aura_incentive)
                    )

                    if amount_to_transfer > 0:
                        pool.to_aura_incentives_usd += amount_to_transfer
                        pool.to_bal_incentives_usd -= amount_to_transfer
                        debt_remaining -= amount_to_transfer
                        transfers_made = True

                if not transfers_made:
                    logger.warning(f"Warning: Could not redistribute AURA debt on {chain.name}. Remaining: {debt_to_aura}")
                    break

    def _filter_dusty_bal_incentives(self):
        """
        Handles dust BAL amounts (<$75). Only moves to AURA if it results in meaningful AURA.
        If a pool ends up with no meaningful incentives after dust handling, redistribute.
        """
        min_aura_incentive = Decimal(self.run_config.fee_config.min_aura_incentive)
        dust_threshold = Decimal(75)
        
        for chain in self.run_config.all_chains:
            pools_to_zero = []
            
            for pool in chain.core_pools:
                if pool.total_to_incentives_usd == 0:
                    continue
                    
                # If pool has dust BAL, try to move to AURA
                if 0 < pool.to_bal_incentives_usd < dust_threshold:
                    potential_aura = pool.to_aura_incentives_usd + pool.to_bal_incentives_usd
                    if potential_aura >= min_aura_incentive:
                        pool.to_aura_incentives_usd = potential_aura
                        pool.to_bal_incentives_usd = Decimal(0)
                
                # After dust handling, if pool has no AURA and only dust BAL, it can't provide meaningful incentives
                if pool.to_aura_incentives_usd < min_aura_incentive and pool.to_bal_incentives_usd < dust_threshold:
                    pools_to_zero.append(pool)
            
            # Redistribute from pools that can't provide meaningful incentives
            if pools_to_zero:
                pools_to_receive = [p for p in chain.core_pools if p not in pools_to_zero and p.total_to_incentives_usd > 0]
                
                if pools_to_receive:
                    total_to_redistribute = sum(p.total_to_incentives_usd for p in pools_to_zero)
                    total_weight = sum(p.total_earned_fees_usd_twap for p in pools_to_receive)
                    
                    # Zero out pools that can't provide meaningful incentives
                    for pool in pools_to_zero:
                        pool.redirected_incentives_usd -= pool.total_to_incentives_usd
                        pool.to_aura_incentives_usd = Decimal(0)
                        pool.to_bal_incentives_usd = Decimal(0)
                        pool.total_to_incentives_usd = Decimal(0)
                    
                    # Redistribute to viable pools
                    for pool in pools_to_receive:
                        weight = pool.total_earned_fees_usd_twap / total_weight
                        amount = total_to_redistribute * weight
                        pool.total_to_incentives_usd += amount
                        pool.redirected_incentives_usd += amount
                        
                        if pool.to_aura_incentives_usd >= pool.to_bal_incentives_usd:
                            pool.to_aura_incentives_usd += amount
                        else:
                            pool.to_bal_incentives_usd += amount

    def generate_bribe_csv(
        self, output_path: Path = Path("fee_allocator/allocations/output_for_msig")
    ) -> Path:
        logger.info("generating bribe csv")
        output = []
        for chain in self.run_config.all_chains:
            for core_pool in chain.core_pools:
                if int(core_pool.total_to_incentives_usd) == 0:
                    continue

                if not core_pool.gauge_address:
                    logger.warning(f"Pool {core_pool.pool_id} has no gauge address")
                
                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "balancer",
                        "amount": round(core_pool.to_bal_incentives_usd, 4),
                        "bribe_platform": core_pool.bribe_platform,
                    },
                )
                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "aura",
                        "amount": round(core_pool.to_aura_incentives_usd, 4),
                        "bribe_platform": core_pool.bribe_platform,
                    },
                )

        noncore_total_to_dao_usd = sum(chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd + chain.partner_noncore_to_dao_usd for chain in self.run_config.all_chains)
        noncore_total_to_beets_usd = sum(chain.noncore_to_beets_usd + chain.alliance_noncore_to_beets_usd + chain.partner_noncore_to_beets_usd for chain in self.run_config.all_chains)
        output.append(
            {
                "target": "0x16b0056636Fcc85f92C49cD49a24bc519d4A1941",  # Balancer Onchain msig
                "platform": "payment",
                "amount": self.run_config.total_to_dao_usd + noncore_total_to_dao_usd,
            }
        )
        output.append(
            {
                "target": self.book["multisigs/beets_treasury"],
                "platform": "beets",
                "amount": self.run_config.total_to_beets_usd + noncore_total_to_beets_usd,
            }
        )

        df = pd.DataFrame(output)
        datetime_file_header = datetime.datetime.fromtimestamp(
            self.date_range[1]
        ).date()
        output_path = PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_bribes_{datetime_file_header}.csv"
        output_path.parent.mkdir(exist_ok=True)

        df.to_csv(
            output_path,
            index=False,
        )
        return output_path

    def generate_incentives_csv(
        self, output_path: Path = Path("fee_allocator/allocations/incentives")
    ) -> Path:
        logger.info("generating incentives csv")
        output = []
        for chain in self.run_config.all_chains:
            for core_pool in chain.core_pools:
                output.append(
                    {
                        "pool_id": core_pool.pool_id,
                        "chain": chain.name,
                        "symbol": core_pool.symbol,
                        "bpt_price": round(core_pool.bpt_price, 4),
                        "earned_fees": round(core_pool.total_earned_fees_usd_twap, 4),
                        "fees_to_vebal": round(core_pool.to_vebal_usd, 4),
                        "fees_to_dao": round(core_pool.to_dao_usd, 4),
                        "fees_to_beets": round(core_pool.to_beets_usd, 4),
                        "total_incentives": round(core_pool.total_to_incentives_usd, 4),
                        "aura_incentives": round(core_pool.to_aura_incentives_usd, 4),
                        "bal_incentives": round(core_pool.to_bal_incentives_usd, 4),
                        "redirected_incentives": round(
                            core_pool.redirected_incentives_usd, 4
                        ),
                        "reroute_incentives": 0,
                        "last_join_exit": core_pool.last_join_exit_ts,
                        "is_partner": any(pool.pool_id == core_pool.pool_id for pool in chain.alliance_pools) or core_pool.pool_id in chain.partner_pools_map,
                    },
                )

        df = pd.DataFrame(output)
        
        sorted_df = df.sort_values(by=["chain", "earned_fees"], ascending=False)
        output_path = (
            PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_incentives_{self.start_date}_{self.end_date}.csv"
        )
        output_path.parent.mkdir(exist_ok=True)

        sorted_df.to_csv(
            output_path,
            index=False,
        )

        return output_path

    def generate_noncore_csv(
        self, output_path: Path = Path("fee_allocator/allocations/noncore")
    ) -> Path:
        logger.info("generating noncore fee allocation csv")
        output = []
        
        for chain in self.run_config.all_chains:
            beets_share_pct = self.run_config.fee_config.beets_share_pct if chain.name == "optimism" else Decimal(0)
            output.append({
                "chain": chain.name,
                "total_fees_collected": round(chain.fees_collected, 4),
                "total_fees_earned_twap": round(chain.total_earned_fees_usd_twap, 4),
                "noncore_fees": round(chain.noncore_fees_collected, 4),
                "noncore_to_dao": round(chain.noncore_to_dao_usd, 4),
                "noncore_to_vebal": round(chain.noncore_to_vebal_usd, 4),
                "noncore_to_beets": round(chain.noncore_to_beets_usd, 4),
                "dao_share_pct": round(self.run_config.fee_config.noncore_dao_share_pct * 100, 2),
                "vebal_share_pct": round(self.run_config.fee_config.noncore_vebal_share_pct * 100, 2),
                "beets_share_pct": round(beets_share_pct * 100, 2)
            })

        df = pd.DataFrame(output)
        output_path = (
            PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_noncore_{self.start_date}_{self.end_date}.csv"
        )
        output_path.parent.mkdir(exist_ok=True)
        
        df.to_csv(output_path, index=False)
        return output_path
    
    def generate_alliance_csv(
        self, output_path: Path = Path("fee_allocator/allocations/alliance")
    ) -> Path:
        logger.info("generating alliance fee distribution csv")
        output = []
        
        for chain in self.run_config.all_chains:
            for alliance_pool in chain.alliance_pools:
                member = next((m for m in self.run_config.alliance_config.alliance_members if alliance_pool.partner == m.name), None)
                if not member:
                    logger.warning(f"Alliance member '{alliance_pool.partner}' not found in alliance config")
                    continue

                core_pool = next((p for p in chain.core_pools if p.pool_id == alliance_pool.pool_id), None)
                noncore_pool = next((p for p in chain.alliance_noncore_fee_data if p.pool_id == alliance_pool.pool_id), None)

                if core_pool:
                    partner_fee = core_pool.to_partner_usd
                    pool_id = core_pool.pool_id
                    pool_type = "core"
                elif noncore_pool:
                    partner_fee = chain.get_alliance_noncore_member_fee(alliance_pool.pool_id)
                    pool_id = noncore_pool.pool_id
                    pool_type = "non-core"
                else:
                    continue

                output.append({
                    "pool_id": pool_id,
                    "chain": chain.name,
                    "alliance_member": alliance_pool.partner,
                    "amount": partner_fee,
                    "target": member.multisig_address,
                    "pool_type": pool_type
                })

        df = pd.DataFrame(output)
        output_path = PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_alliance_{self.start_date}_{self.end_date}.csv"
        output_path.parent.mkdir(exist_ok=True)
        df.to_csv(output_path, index=False)
        return output_path

    def generate_partner_csv(
        self, output_path: Path = Path("fee_allocator/allocations/partner")
    ) -> Path:
        logger.info("generating partner fee distribution csv")
        output = []

        if self.run_config.partner_config and self.run_config.partner_config.partners:
            for chain in self.run_config.all_chains:
                # Core partner pools
                for pool in chain.core_pools:
                    if pool.partner:
                        output.append({
                            "pool_id": pool.pool_id,
                            "chain": chain.name,
                            "partner": pool.partner.name,
                            "earned_fees": pool.total_earned_fees_usd_twap,
                            "amount": pool.to_partner_usd,
                            "target": pool.partner.multisig_address,
                            "pool_type": "core"
                        })

                # Non-core partner pools
                for pool_data in chain.partner_noncore_fee_data:
                    if pool_data.partner:
                        partner_fee = chain.get_partner_noncore_fee(pool_data.pool_id)
                        if partner_fee > 0:
                            output.append({
                                "pool_id": pool_data.pool_id,
                                "chain": chain.name,
                                "partner": pool_data.partner.name,
                                "earned_fees": pool_data.total_earned_fees_usd_twap,
                                "amount": partner_fee,
                                "target": pool_data.partner.multisig_address,
                                "pool_type": "non-core"
                            })

        df = pd.DataFrame(output)
        output_path = PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_partner_{self.start_date}_{self.end_date}.csv"
        output_path.parent.mkdir(exist_ok=True)
        df.to_csv(output_path, index=False)
        return output_path

    def generate_bribe_payload(
        self,
        input_csv: str,
        output_path: Path = Path("fee_allocator/payloads"),
        partner_csv: str = None,
        alliance_csv: str = None,
        include_bal_transfer: bool = True
    ) -> Path:
        """builds a safe payload from the bribe csv
        
        Args:
            input_csv: Path to the bribe CSV file
            output_path: Directory to save the payload JSON
            partner_csv: Optional path to partner CSV file
            alliance_csv: Optional path to alliance CSV file
            include_bal_transfer: Whether to include BAL transfer to veBAL (default True)
                                Set to False for v2 in combined mode to avoid duplication
        """
        logger.info("generating payload")
        builder = SafeTxBuilder(self.book["multisigs/fees"])
        usdc = SafeContract(self.book["tokens/USDC"], abi_file_path=f"{base_dir}/abi/ERC20.json")
        bal = SafeContract(self.book["tokens/BAL"], abi_file_path=f"{base_dir}/abi/ERC20.json")
        aura_bribe_market = SafeContract(
            self.book["hidden_hand2/aura_briber"],
            abi_file_path=f"{base_dir}/abi/bribe_market.json",
        )
        bal_bribe_market = SafeContract(
            self.book["hidden_hand2/balancer_briber"],
            abi_file_path=f"{base_dir}/abi/bribe_market.json",
        )

        df = pd.read_csv(input_csv)
        
        bribe_df = df[df["platform"].isin(["balancer", "aura"])]
        payment_df = df[df["platform"] == "payment"].iloc[0]
        beets_df = df[df["platform"] == "beets"].iloc[0]

        hh_bribe_df = bribe_df[bribe_df["bribe_platform"] == "hiddenhand"]
        paladin_bribe_df = bribe_df[bribe_df["bribe_platform"] == "paladin"]

        total_hh_bribe_usdc = int(hh_bribe_df["amount"].sum() * 1e6)
        total_paladin_bribe_usdc = int(paladin_bribe_df["amount"].sum() * 1e6)
        
        dao_fee_usdc = round(payment_df["amount"] * 1e6) - 1000  # round down 0.1 cent
        beets_fee_usdc = round(beets_df["amount"] * 1e6) - 1000  # round down 0.1 cent

        if total_hh_bribe_usdc > 0:
            self._process_hiddenhand_bribes(
                hh_bribe_df,
                total_hh_bribe_usdc,
                usdc,
                bal_bribe_market,
                aura_bribe_market
            )
        
        if total_paladin_bribe_usdc > 0:
            self._process_paladin_quests(
                paladin_bribe_df,
                usdc
            )

        usdc.transfer(payment_df["target"], dao_fee_usdc)
        usdc.transfer(beets_df["target"], beets_fee_usdc)

        alliance_fee_usdc_spent = 0
        if alliance_csv:
            try:
                alliance_df = pd.read_csv(alliance_csv)
                for _, row in alliance_df.iterrows():
                    if row["amount"] > 0:
                        alliance_amount = round(row["amount"] * 1e6)
                        alliance_fee_usdc_spent += alliance_amount
                        usdc.transfer(row["target"], alliance_amount)
            except pd.errors.EmptyDataError:
                logger.info(f"no alliance members found for protocol {self.run_config.protocol_version}")
        
        partner_fee_usdc_spent = 0
        if partner_csv:
            try:
                partner_df = pd.read_csv(partner_csv)
                for _, row in partner_df.iterrows():
                    if row["amount"] > 0:
                        partner_amount = round(row["amount"] * 1e6)
                        partner_fee_usdc_spent += partner_amount
                        usdc.transfer(row["target"], partner_amount)
            except pd.errors.EmptyDataError:
                logger.info(f"no partners found for protocol {self.run_config.protocol_version}")

        datetime_file_header = datetime.datetime.fromtimestamp(self.date_range[1]).date()

        vebal_usdc_amount = round(float(self.run_config.total_to_vebal_usd) * 1e6)
        
        # Get BAL balance (only if enabled)
        if include_bal_transfer:
            vebal_bal_amount = (
                self.run_config.mainnet.web3.eth.contract(bal.address, abi=get_abi("ERC20"))
                .functions.balanceOf(builder.safe_address)
                .call()
            )
        else:
            vebal_bal_amount = 0

        # Transfer to veBAL injector
        if vebal_usdc_amount > 0:
            usdc.transfer(self.book["maxiKeepers/veBalFeeInjector"], vebal_usdc_amount)
        if vebal_bal_amount > 0:
            bal.transfer(self.book["maxiKeepers/veBalFeeInjector"], vebal_bal_amount)

        # Save payload with protocol version prefix
        output_path = PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_{datetime_file_header}.json"
        builder.output_payload(output_path)
        
        return output_path
    
    def _check_paladin_gauge_requirements(self):
        """Check Paladin gauges for requirements and log issues"""
        
        with open(f"{base_dir}/abi/gauge.json", "r") as f:
            gauge_abi = json.load(f)
        
        w3 = self.run_config.mainnet.web3
        usdc = Web3.to_checksum_address(self.book["tokens/USDC"])
        gauges_with_issues = []
        
        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                if pool.bribe_platform != "paladin":
                    continue
                    
                gauge = Web3.to_checksum_address(pool.gauge_address)
                contract = w3.eth.contract(address=gauge, abi=gauge_abi)
                
                has_issue = False
                action_needed = []
                
                try:
                    usdc_found = usdc in [contract.functions.reward_tokens(i).call() for i in range(8)]

                    has_correct_distributor = False
                    if usdc_found:
                        distributor = contract.functions.reward_data(usdc).call()[1]
                        has_correct_distributor = (
                            distributor.lower() == self.book["paladin/QuestBoardV2_1"].lower() or
                            distributor.lower() == self.book["paladin/QuestBoardV2_1Aura"].lower()
                        )
                    
                    if not usdc_found or not has_correct_distributor:
                        has_issue = True
                        
                        distributors_needed = []
                        if pool.to_bal_incentives_usd > 0:
                            distributors_needed.append(f"Balancer distributor ({self.book['paladin/QuestBoardV2_1']})")
                        if pool.to_aura_incentives_usd > 0:
                            distributors_needed.append(f"Aura distributor ({self.book['paladin/QuestBoardV2_1Aura']})")
                        
                        if distributors_needed:
                            if not usdc_found:
                                action_needed.append(f"Add USDC ({usdc}) as reward token and set {' and '.join(distributors_needed)}")
                            else:
                                action_needed.append(f"Set {' and '.join(distributors_needed)}")
                                
                except Exception:
                    has_issue = True
                    action_needed.append("Gauge has incompatible implementation")

                if has_issue:
                    logger.warning(f"Paladin gauge {pool.gauge_address} missing requirements: {'. '.join(action_needed)}")
                    gauges_with_issues.append({
                        "gauge": pool.gauge_address,
                        "pool_id": pool.pool_id,
                        "chain": chain.name,
                        "action": ". ".join(action_needed),
                        "amount": float(pool.total_to_incentives_usd)
                    })

        if gauges_with_issues:
            issues_file = base_dir / "allocations" / f"{self.run_config.protocol_version}_paladin_gauge_status_{self.start_date}_{self.end_date}.json"
            with open(issues_file, "w") as f:
                json.dump(gauges_with_issues, f, indent=2)
    
    
    def _process_hiddenhand_bribes(self, bribe_df, total_bribe_usdc, usdc, bal_bribe_market, aura_bribe_market):
        usdc.approve(self.book["hidden_hand2/bribe_vault"], total_bribe_usdc + 1)  # 1 wei buffer
        
        for _, row in bribe_df.iterrows():
            if int(row["amount"]) == 0:
                continue
                
            prop_hash = self._get_prop_hash(row["platform"], row["target"])
            mantissa = round(row["amount"] * 1e6)
            
            if row["platform"] == "balancer":
                bal_bribe_market.depositBribe(prop_hash, self.book["tokens/USDC"], mantissa, 0, 2)
            elif row["platform"] == "aura":
                aura_bribe_market.depositBribe(prop_hash, self.book["tokens/USDC"], mantissa, 0, 1)
    
    def _process_paladin_quests(self, bribe_df, usdc):
        
        valid_bribes = bribe_df[bribe_df["amount"] > 0]
        
        with open(f"{base_dir}/abi/paladin_quest_board.json", "r") as f:
            paladin_abi = json.load(f)

        quest_boards = {}
        platform_fee_ratios = {}
        
        for platform in ["balancer", "aura"]:
            bribes = valid_bribes[valid_bribes["platform"] == platform]
            if bribes.empty:
                continue
                
            quest_board_addr = self.book["paladin/QuestBoardV2_1"] if platform == "balancer" else self.book["paladin/QuestBoardV2_1Aura"]
            quest_boards[platform] = SafeContract(quest_board_addr, abi=paladin_abi)
            
            w3_contract = self.run_config.mainnet.web3.eth.contract(
                address=quest_board_addr,
                abi=paladin_abi
            )
            try:
                platform_fee_ratios[platform] = w3_contract.functions.platformFeeRatio().call()
            except Exception:
                platform_fee_ratios[platform] = 400  # 4% default
            
            total = sum(round(row["amount"] * 1e6) for _, row in bribes.iterrows())
            usdc.approve(quest_board_addr, total)
        
        for _, row in valid_bribes.iterrows():
            mantissa = round(row["amount"] * 1e6)
            platform = row["platform"]
            quest_board = quest_boards[platform]
            fee_ratio = platform_fee_ratios[platform]
            
            total_reward_amount = int(mantissa * 10000 / (10000 + fee_ratio))
            fee_amount = mantissa - total_reward_amount
            
            quest_board.createRangedQuest(
                row["target"],               # gauge
                self.book["tokens/USDC"],    # rewardToken
                True,                        # startNextPeriod
                2,                           # duration (2 weeks)
                1,                           # minRewardPerVote 
                total_reward_amount,         # maxRewardPerVote
                total_reward_amount,         # totalRewardAmount
                fee_amount,                  # feeAmount
                0,                           # voteType (NORMAL)
                1,                           # closeType (ROLLOVER)
                []                           # voterList
            )


    @staticmethod
    def _get_prop_hash(platform: str, target: str) -> str:
        if platform == "balancer":
            prop = Web3.solidity_keccak(["address"], [Web3.to_checksum_address(target)])
            return f"0x{prop.hex().replace('0x', '')}"
        if platform == "aura":
            return get_hh_aura_target(target)
        raise ValueError(f"platform {platform} not supported")

    def recon(self) -> None:
        """
        Reconciles and validates fee distribution results.
        Checks:
        1. No negative incentive amounts
        2. Sum of percentage allocations equals 1
        3. Aura veBAL share within target range
        4. Small delta between collected and distributed fees
        """
        total_fees = self.run_config.total_fees_collected_usd
        total_aura = Decimal(0)
        total_bal = Decimal(0)
        total_dao = Decimal(0)
        total_vebal = Decimal(0)
        total_partner = Decimal(0)
        total_distributed = Decimal(0)
        total_beets = Decimal(0)

        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                assert pool.to_aura_incentives_usd >= 0, f"Negative aura incentives: {pool.to_aura_incentives_usd}"
                assert pool.to_bal_incentives_usd >= 0, f"Negative bal incentives: {pool.to_bal_incentives_usd}"
                assert pool.to_dao_usd >= 0, f"Negative dao share: {pool.to_dao_usd}"
                assert pool.to_vebal_usd >= 0, f"Negative vebal share: {pool.to_vebal_usd}"
                assert pool.to_partner_usd >= 0, f"Negative partner share: {pool.to_partner_usd}"
                assert pool.to_beets_usd >= 0, f"Negative beets share: {pool.to_beets_usd}"

                total_aura += pool.to_aura_incentives_usd
                total_bal += pool.to_bal_incentives_usd
                total_dao += pool.to_dao_usd
                total_vebal += pool.to_vebal_usd
                total_partner += pool.to_partner_usd
                total_beets += pool.to_beets_usd

            total_dao += chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd + chain.partner_noncore_to_dao_usd
            total_vebal += chain.noncore_to_vebal_usd + chain.alliance_noncore_to_vebal_usd + chain.partner_noncore_to_vebal_usd
            total_beets += chain.noncore_to_beets_usd + chain.alliance_noncore_to_beets_usd + chain.partner_noncore_to_beets_usd

            for noncore_pool in chain.alliance_noncore_fee_data:
                total_partner += chain.get_alliance_noncore_member_fee(noncore_pool.pool_id)
            
            for noncore_pool in chain.partner_noncore_fee_data:
                total_partner += chain.get_partner_noncore_fee(noncore_pool.pool_id)

        # Total distributed includes all allocations including partner fees
        total_distributed = total_aura + total_bal + total_dao + total_vebal + total_partner + total_beets

        # For percentage calculations, we need to check that everything sums to 100%
        if total_distributed > 0:
            total_pct = total_distributed / total_distributed  # This should always be 1
            assert abs(1 - total_pct) < Decimal('0.0001'), f"Percentages don't sum to 1: {total_pct}"

        # Only check Aura share against BAL for core pool incentives
        core_pool_incentives = total_aura + total_bal
        aura_share = total_aura / core_pool_incentives if core_pool_incentives > 0 else Decimal(0)

        total_core_fees_collected = Decimal(0)
        for chain in self.run_config.all_chains:
            # Core pools get their share of collected fees based on earned/total_earned ratio
            if chain.total_fees_earned > 0:
                core_share = chain.total_earned_fees_usd_twap / chain.total_fees_earned
                total_core_fees_collected += chain.fees_collected * core_share
        
        total_noncore_fees_collected = Decimal(0)
        for chain in self.run_config.all_chains:
            # Non-core fees are what's left after core pools
            if chain.total_fees_earned > 0:
                noncore_share = (chain.noncore_fees_collected + chain.alliance_noncore_fees_earned + chain.partner_noncore_fees_earned) / chain.total_fees_earned
                total_noncore_fees_collected += chain.fees_collected * noncore_share
        
        summary = {
            "feesCollected": float(round(total_fees, 2)),
            "totalDistributed": float(round(total_distributed, 2)),
            "feesNotDistributed": float(round(total_fees - total_distributed, 2)),
            "coreFees": float(round(total_core_fees_collected, 2)),
            "noncoreFees": float(round(total_noncore_fees_collected, 2)),
            "auraIncentives": float(round(total_aura, 2)),
            "balIncentives": float(round(total_bal, 2)),
            "feesToDao": float(round(total_dao, 2)),
            "feesToVebal": float(round(total_vebal, 2)),
            "feesToPartners": float(round(total_partner, 2)),
            "feesToBeets": float(round(total_beets, 2)),
            "auravebalShare": float(round(aura_share, 2)),
            "auraIncentivesPct": float(round(total_aura / total_distributed, 4)) if total_distributed > 0 else 0,
            "balIncentivesPct": float(round(total_bal / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToDaoPct": float(round(total_dao / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToVebalPct": float(round(total_vebal / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToPartnersPct": float(round(total_partner / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToBeetsPct": float(round(total_beets / total_distributed, 4)) if total_distributed > 0 else 0,
            "createdAt": int(datetime.datetime.now().timestamp()),
            "periodStart": self.date_range[0],
            "periodEnd": self.date_range[1]
        }

        recon_file = Path(PROJECT_ROOT) / "fee_allocator/summaries" / f"{self.run_config.protocol_version}_recon.json"
        recon_file.parent.mkdir(exist_ok=True)

        if recon_file.exists():
            with open(recon_file) as f:
                data = json.load(f)
        else:
            data = []

        data.append(summary)
        with open(recon_file, "w") as f:
            json.dump(data, f, indent=2)

    def generate_report(self, payload_path: Path, fee_files: List[Path] = None) -> Path:
        """
        Generate a markdown report for the payload.
        """
        payload_name = payload_path.stem
        if payload_name.startswith(("v2_", "v3_")):
            date_str = payload_name[3:]
        else:
            date_str = payload_name
            
        if self.run_config.protocol_version:
            report_name = f"{self.run_config.protocol_version}_{date_str}.md"
        else:
            report_name = f"{date_str}.md"
            
        reports_dir = Path(PROJECT_ROOT) / "fee_allocator" / "reports"
        report_path = reports_dir / report_name
        
        gauge_issues_path = Path(PROJECT_ROOT) / f"fee_allocator/allocations/{self.run_config.protocol_version}_paladin_gauge_status_{self.start_date}_{self.end_date}.json"
        if not gauge_issues_path.exists():
            gauge_issues_path = None
        
        return save_markdown_report(payload_path, fee_files, output_path=report_path, gauge_issues_path=gauge_issues_path)