from typing import TypedDict, Union, Dict, List
from bal_tools.subgraph import DateRange
from bal_tools.safe_tx_builder import SafeTxBuilder, SafeContract
from bal_addresses import AddrBook
from bal_tools.utils import get_abi
import pandas as pd
from decimal import Decimal
import datetime
from pathlib import Path
from dotenv import load_dotenv
import json

from fee_allocator.accounting.chains import CorePoolRunConfig
from fee_allocator.accounting.core_pools import PoolFee
from fee_allocator.accounting import PROJECT_ROOT
from fee_allocator.logger import logger
from fee_allocator.payload_visualizer import save_markdown_report
from fee_allocator.bribe_platforms import get_platform

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
        self.start_date = datetime.datetime.fromtimestamp(date_range[0], tz=datetime.timezone.utc).date()
        self.end_date = datetime.datetime.fromtimestamp(date_range[1], tz=datetime.timezone.utc).date()
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
        self.run_config.set_initial_pool_allocation()
        if redistribute:
            self.redistribute_fees()

    def redistribute_fees(self):
        """
        Redistributes fees among pools based on minimum incentive amounts.

        Pools with total incentives below the minimum threshold get their incentives
        redistributed to eligible pools above the threshold.

        """
        min_amount = self.run_config.fee_config.min_aura_incentive
        logger.info(f"Redistribution threshold: ${min_amount}")

        for chain in self.run_config.all_chains:
            pools_to_redistribute = [p for p in chain.core_pools if p.total_to_incentives_usd < min_amount]
            pools_to_receive = [p for p in chain.core_pools if p.total_to_incentives_usd >= min_amount]

            if not pools_to_receive:
                for pool in pools_to_redistribute:
                    amount = pool.total_to_incentives_usd
                    pool.to_dao_usd += amount * self.run_config.fee_config.noncore_dao_share_pct
                    pool.to_vebal_usd += amount * self.run_config.fee_config.noncore_vebal_share_pct
                    pool.redirected_incentives_usd -= amount
                    pool.total_to_incentives_usd = Decimal(0)
                continue

            total_fees_to_redistribute = sum(p.total_to_incentives_usd for p in pools_to_redistribute)
            total_weight = sum(p.total_earned_fees_usd_twap for p in pools_to_receive)

            for pool in pools_to_redistribute:
                pool.redirected_incentives_usd -= pool.total_to_incentives_usd
                pool.total_to_incentives_usd = Decimal(0)

            for pool in pools_to_receive:
                weight = pool.total_earned_fees_usd_twap / total_weight
                total = total_fees_to_redistribute * weight
                pool.total_to_incentives_usd += total
                pool.redirected_incentives_usd += total

    def generate_artifacts(self, include_bal_transfer: bool = True) -> Dict[str, Path]:
        """
        Generates all fee allocation artifacts (CSVs and payload).
        """
        logger.info("generating fee allocation artifacts")

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
                        "amount": round(core_pool.total_to_incentives_usd, 4),
                        "is_alliance": core_pool.is_alliance_core_pool,
                        "voting_pool_override": core_pool.voting_pool_override,
                    },
                )

        noncore_total_to_dao_usd = sum(chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd + chain.partner_noncore_to_dao_usd for chain in self.run_config.all_chains)
        noncore_total_to_beets_usd = sum(chain.noncore_to_beets_usd + chain.alliance_noncore_to_beets_usd + chain.partner_noncore_to_beets_usd for chain in self.run_config.all_chains)
        output.append(
            {
                "target": "0x16b0056636Fcc85f92C49cD49a24bc519d4A1941",  # Balancer Onchain Ltd msig
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
            self.date_range[1], tz=datetime.timezone.utc
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
                        "redirected_incentives": round(
                            core_pool.redirected_incentives_usd, 4
                        ),
                        "last_join_exit": core_pool.last_join_exit_ts,
                        "is_alliance": core_pool.is_alliance_core_pool,
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

        df = pd.read_csv(input_csv)

        bribe_df = df[~df["platform"].isin(["payment", "beets"]) if "platform" in df.columns else df["amount"] > 0]
        bribe_df = bribe_df[bribe_df["amount"] > 0]
        payment_df = df[df["platform"] == "payment"].iloc[0]
        beets_df = df[df["platform"] == "beets"].iloc[0]

        dao_fee_usdc = round(payment_df["amount"] * 1e6) - 1000  # round down 0.1 cent
        beets_fee_usdc = round(beets_df["amount"] * 1e6) - 1000  # round down 0.1 cent

        if not bribe_df.empty:
            platform = get_platform(self.book, self.run_config)
            platform.process_bribes(bribe_df, builder, usdc)

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

        datetime_file_header = datetime.datetime.fromtimestamp(self.date_range[1], tz=datetime.timezone.utc).date()

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

    def recon(self) -> None:
        """
        Reconciles and validates fee distribution results.
        Checks:
        1. No negative incentive amounts
        2. Sum of percentage allocations equals 1
        3. Small delta between collected and distributed fees
        """
        total_fees = self.run_config.total_fees_collected_usd
        total_incentives = Decimal(0)
        total_dao = Decimal(0)
        total_vebal = Decimal(0)
        total_partner = Decimal(0)
        total_distributed = Decimal(0)
        total_beets = Decimal(0)

        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                assert pool.total_to_incentives_usd >= 0, f"Negative incentives: {pool.total_to_incentives_usd}"
                assert pool.to_dao_usd >= 0, f"Negative dao share: {pool.to_dao_usd}"
                assert pool.to_vebal_usd >= 0, f"Negative vebal share: {pool.to_vebal_usd}"
                assert pool.to_partner_usd >= 0, f"Negative partner share: {pool.to_partner_usd}"
                assert pool.to_beets_usd >= 0, f"Negative beets share: {pool.to_beets_usd}"

                total_incentives += pool.total_to_incentives_usd
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

        total_distributed = total_incentives + total_dao + total_vebal + total_partner + total_beets

        if total_distributed > 0:
            total_pct = total_distributed / total_distributed
            assert abs(1 - total_pct) < Decimal('0.0001'), f"Percentages don't sum to 1: {total_pct}"

        total_core_fees_collected = Decimal(0)
        for chain in self.run_config.all_chains:
            if chain.total_fees_earned > 0:
                core_share = chain.total_earned_fees_usd_twap / chain.total_fees_earned
                total_core_fees_collected += chain.fees_collected * core_share

        total_noncore_fees_collected = Decimal(0)
        for chain in self.run_config.all_chains:
            if chain.total_fees_earned > 0:
                noncore_share = (chain.noncore_fees_collected + chain.alliance_noncore_fees_earned + chain.partner_noncore_fees_earned) / chain.total_fees_earned
                total_noncore_fees_collected += chain.fees_collected * noncore_share

        summary = {
            "feesCollected": float(round(total_fees, 2)),
            "totalDistributed": float(round(total_distributed, 2)),
            "feesNotDistributed": float(round(total_fees - total_distributed, 2)),
            "coreFees": float(round(total_core_fees_collected, 2)),
            "noncoreFees": float(round(total_noncore_fees_collected, 2)),
            "totalIncentives": float(round(total_incentives, 2)),
            "feesToDao": float(round(total_dao, 2)),
            "feesToVebal": float(round(total_vebal, 2)),
            "feesToPartners": float(round(total_partner, 2)),
            "feesToBeets": float(round(total_beets, 2)),
            "incentivesPct": float(round(total_incentives / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToDaoPct": float(round(total_dao / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToVebalPct": float(round(total_vebal / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToPartnersPct": float(round(total_partner / total_distributed, 4)) if total_distributed > 0 else 0,
            "feesToBeetsPct": float(round(total_beets / total_distributed, 4)) if total_distributed > 0 else 0,
            "createdAt": int(datetime.datetime.now().timestamp()),
            "periodStart": self.date_range[0],
            "periodEnd": self.date_range[1],
            "bribeThreshold": self.run_config.fee_config.min_aura_incentive
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

        return save_markdown_report(payload_path, fee_files, output_path=report_path)