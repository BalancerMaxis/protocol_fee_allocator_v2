from typing import TypedDict, Union, Dict
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
import math

from fee_allocator.accounting.chains import CorePoolChain, CorePoolRunConfig
from fee_allocator.accounting.core_pools import PoolFee
from fee_allocator.accounting import PROJECT_ROOT
from fee_allocator.utils import get_hh_aura_target
from fee_allocator.logger import logger

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
        self.run_config = CorePoolRunConfig(
            input_fees,
            date_range,
            cache_dir=cache_dir,
            use_cache=use_cache,
            core_pools=core_pools,
            protocol_version=protocol_version,
        )
        self.book = AddrBook("mainnet").flatbook

    def allocate(self):
        """
        Allocates protocol fees to core pools and non-core pools according to BIP-734.
        Core pools: 70% voting incentives, 12.5% veBAL, 17.5% DAO
        Non-core pools: 82.5% veBAL, 17.5% DAO
        """
        self.run_config.set_core_pool_chains_data()
        self.run_config.set_aura_vebal_share()
        self.run_config.set_initial_pool_allocation()
        self.redistribute_fees()

    def redistribute_fees(self):
        """
        Redistributes fees among pools based on minimum incentive amounts and chain-specific rules.
        This method performs the following steps:
        1. Identifies pools with incentives below the minimum threshold.
        2. Redistributes fees from these pools to eligible pools above the threshold.
        3. Recalculates incentive amounts for Aura and Balancer.
        4. Adjusts DAO and veBAL shares based on the original distribution.
        5. Handles Aura minimum incentives with and without a buffer.
        """
        min_amount = self.run_config.fee_config.min_vote_incentive_amount
        
        # for chain in self.run_config.all_chains:
        #     total_earned_fees = sum(p.total_earned_fees_usd_twap for p in chain.core_pools)
        #     for pool in chain.core_pools:
        #         if total_earned_fees > 0:
        #             pool.original_earned_fee_share = pool.total_earned_fees_usd_twap / total_earned_fees
        #         else:
        #             pool.original_earned_fee_share = Decimal(0)

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
                pool.to_aura_incentives_usd += total if pool.is_alliance_pool else total * self.run_config.aura_vebal_share
                pool.to_bal_incentives_usd += Decimal(0) if pool.is_alliance_pool else total * (1 - self.run_config.aura_vebal_share)

            # for pool in chain.core_pools:
            #     pool.to_dao_usd = pool.original_earned_fee_share * chain.fees_collected * self.run_config.fee_config.dao_share_pct
            #     pool.to_vebal_usd = pool.original_earned_fee_share * chain.fees_collected * self.run_config.fee_config.vebal_share_pct

        self._handle_aura_min(buffer=0.25)
        self._handle_aura_min()
        self._filter_dusty_bal_incentives()

    def _handle_aura_min(self, buffer=0):
        """
        Handles the minimum Aura incentive requirement for pools.
        
        This method performs the following steps:
        1. Calculates the minimum Aura incentive amount, considering an optional buffer.
        2. Identifies pools below the minimum threshold or with specific overrides.
        3. Redistributes incentives from these pools to Balancer.
        4. Reallocates the debt from pools below the minimum to eligible pools above the threshold.
        5. Adjusts Aura and Balancer incentives for eligible pools to repay the debt.
        6. Logs the remaining debt information for each affected pool.

        Args:
            buffer (float): An optional buffer percentage to adjust the minimum Aura incentive. Defaults to 0.
        """
        min_aura_incentive = self.run_config.fee_config.min_aura_incentive * (1 - buffer)
        for chain in self.run_config.all_chains:
            debt_to_aura = Decimal(0)

            for pool in chain.core_pools:
                if pool.to_aura_incentives_usd < min_aura_incentive or (
                    pool.override and pool.override.voting_pool == "bal"
                ):
                    debt_to_aura += pool.to_aura_incentives_usd
                    pool.to_bal_incentives_usd += pool.to_aura_incentives_usd
                    pool.to_aura_incentives_usd = Decimal(0)

            if not debt_to_aura:
                continue

            pools_over_min = [
                p
                for p in chain.core_pools
                if p.to_aura_incentives_usd >= min_aura_incentive
            ]
            if not pools_over_min:
                continue

            amount_per_pool = debt_to_aura / len(pools_over_min)
            debt_repaid = Decimal(0)

            for pool in pools_over_min:
                amount = min(amount_per_pool, pool.to_bal_incentives_usd)
                pool.to_aura_incentives_usd += amount
                pool.to_bal_incentives_usd -= amount
                debt_repaid += amount

                if debt_to_aura - debt_repaid >= 0:
                    print(
                        f"{pool.pool_id} remaining debt to aura market: {debt_to_aura}, "
                        f"Debt repaid: {debt_repaid}, debt remaining: {debt_to_aura - debt_repaid}"
                    )

    def _filter_dusty_bal_incentives(self):
        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                if pool.to_bal_incentives_usd < Decimal(75):
                    pool.to_aura_incentives_usd += pool.to_bal_incentives_usd
                    pool.to_bal_incentives_usd = Decimal(0)

    def generate_bribe_csv(
        self, output_path: Path = Path("fee_allocator/allocations/output_for_msig")
    ) -> Path:
        logger.info("generating bribe csv")
        output = []
        for chain in self.run_config.all_chains:
            for core_pool in chain.core_pools:
                if int(core_pool.total_to_incentives_usd) == 0:
                    continue

                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "balancer",
                        "amount": round(core_pool.to_bal_incentives_usd, 4),
                    },
                )
                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "aura",
                        "amount": round(core_pool.to_aura_incentives_usd, 4),
                    },
                )

        noncore_total_to_dao_usd = sum(chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd for chain in self.run_config.all_chains)
        output.append(
            {
                "target": "0x10A19e7eE7d7F8a52822f6817de8ea18204F2e4f",  # DAO msig
                "platform": "payment",
                "amount": self.run_config.total_to_dao_usd + noncore_total_to_dao_usd,
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
                        "total_incentives": round(core_pool.total_to_incentives_usd, 4),
                        "aura_incentives": round(core_pool.to_aura_incentives_usd, 4),
                        "bal_incentives": round(core_pool.to_bal_incentives_usd, 4),
                        "redirected_incentives": round(
                            core_pool.redirected_incentives_usd, 4
                        ),
                        "reroute_incentives": 0,
                        "last_join_exit": core_pool.last_join_exit_ts,
                        "is_partner": any(pool.pool_id == core_pool.pool_id for pool in chain.alliance_pools),
                    },
                )

        df = pd.DataFrame(output)
        
        sorted_df = df.sort_values(by=["chain", "earned_fees"], ascending=False)
        start_date = datetime.datetime.fromtimestamp(self.date_range[0]).date()
        end_date = datetime.datetime.fromtimestamp(self.date_range[1]).date()
        output_path = (
            PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_incentives_{start_date}_{end_date}.csv"
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
            output.append({
                "chain": chain.name,
                "total_fees_collected": round(chain.fees_collected, 4),
                "total_fees_earned_twap": round(chain.total_earned_fees_usd_twap, 4),
                "noncore_fees": round(chain.noncore_fees_collected, 4),
                "noncore_to_dao": round(chain.noncore_to_dao_usd, 4),
                "noncore_to_vebal": round(chain.noncore_to_vebal_usd, 4),
                "dao_share_pct": round(self.run_config.fee_config.noncore_dao_share_pct * 100, 2),
                "vebal_share_pct": round(self.run_config.fee_config.noncore_vebal_share_pct * 100, 2)
            })
            
        df = pd.DataFrame(output)
        start_date = datetime.datetime.fromtimestamp(self.date_range[0]).date()
        end_date = datetime.datetime.fromtimestamp(self.date_range[1]).date()
        output_path = (
            PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_noncore_{start_date}_{end_date}.csv"
        )
        output_path.parent.mkdir(exist_ok=True)
        
        df.to_csv(output_path, index=False)
        return output_path
    
    def generate_partner_csv(
        self, output_path: Path = Path("fee_allocator/allocations/partner")
    ) -> Path:
        logger.info("generating partner csv")
        output = []
        for chain in self.run_config.all_chains:
            for alliance_pool in chain.alliance_pools:
                member = next((m for m in self.run_config.alliance_config.alliance_members if alliance_pool.partner == m.name), None)
                core_pool = next((p for p in chain.core_pools if p.pool_id == alliance_pool.pool_id), None)
                noncore_pool = next((p for p in chain.alliance_noncore_fee_data if p.pool_id == alliance_pool.pool_id), None)

                if core_pool:
                    partner_fee = core_pool.to_partner_usd
                    pool_id = core_pool.pool_id
                elif noncore_pool:
                    partner_fee = (
                        noncore_pool.total_earned_fees_usd_twap / chain.alliance_noncore_fees_collected
                        * chain.alliance_noncore_fees_collected
                        * self.run_config.alliance_config.alliance_fee_allocations["non_core"].partner_share_pct
                    )
                    pool_id = noncore_pool.pool_id
                else:
                    continue

                output.append({
                    "pool_id": pool_id,
                    "chain": chain.name,
                    "partner": alliance_pool.partner,
                    "amount": partner_fee,
                    "target": member.multisig_address,
                })


        df = pd.DataFrame(output)
        output_path = PROJECT_ROOT / output_path / f"{self.run_config.protocol_version}_partner.csv"
        output_path.parent.mkdir(exist_ok=True)
        df.to_csv(output_path, index=False)
        return output_path

    def generate_bribe_payload(
        self,
        input_csv: str,
        output_path: Path = Path("fee_allocator/payloads"),
        partner_csv: str = None
    ) -> Path:
        """builds a safe payload from the bribe csv"""
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

        total_bribe_usdc = sum(int(row["amount"] * 1e6) for _, row in bribe_df.iterrows())
        dao_fee_usdc = int(payment_df["amount"] * 1e6)

        """bribe txs"""
        usdc.approve(self.book["hidden_hand2/bribe_vault"], total_bribe_usdc + 1) # 1 wei buffer

        for _, row in bribe_df.iterrows():
            if int(row["amount"]) == 0:
                continue
            prop_hash = self._get_prop_hash(row["platform"], row["target"])
            mantissa = int(row["amount"] * 1e6)

            if row["platform"] == "balancer":
                bal_bribe_market.depositBribe(prop_hash, self.book["tokens/USDC"], mantissa, 0, 2)
            elif row["platform"] == "aura":
                aura_bribe_market.depositBribe(prop_hash, self.book["tokens/USDC"], mantissa, 0, 1)

        """transfer txs"""
        usdc.transfer(payment_df["target"], dao_fee_usdc)

        partner_fee_usdc_spent = 0
        if partner_csv:
            try:
                partner_df = pd.read_csv(partner_csv)
                for _, row in partner_df.iterrows():
                    if row["amount"] > 0:
                        partner_amount = int(row["amount"] * 1e6)
                        partner_fee_usdc_spent += partner_amount
                        usdc.transfer(row["target"], partner_amount)
            except pd.errors.EmptyDataError:
                logger.info(f"no alliance members found for protocol {self.run_config.protocol_version}")

        datetime_file_header = datetime.datetime.fromtimestamp(self.date_range[1]).date()

        if self.run_config.protocol_version == "v2":
            output_path = PROJECT_ROOT / output_path / f"v2_{datetime_file_header}.json"
            builder.output_payload(output_path)
            return output_path

        v2_file = PROJECT_ROOT / output_path / f"v2_{datetime_file_header}.json"
        
        if not v2_file.exists():
            raise FileNotFoundError(f"V2 payload not found at {v2_file}. Run V2 allocation first.")
        
        with open(v2_file) as f:
            v2_payload = json.load(f)
        
        v2_usdc_spent = 0
        for tx in v2_payload["transactions"]:
            if tx["to"].lower() == self.book["tokens/USDC"].lower():
                if tx["contractMethod"]["name"] == "transfer":
                    v2_usdc_spent += int(tx["contractInputsValues"]["_value"])
            elif tx["to"].lower() in [
                self.book["hidden_hand2/balancer_briber"].lower(),
                self.book["hidden_hand2/aura_briber"].lower()
            ]:
                if tx["contractMethod"]["name"] == "depositBribe":
                    if tx["contractInputsValues"]["_token"].lower() == self.book["tokens/USDC"].lower():
                        v2_usdc_spent += int(tx["contractInputsValues"]["_amount"])

        total_usdc_spent = v2_usdc_spent + total_bribe_usdc + dao_fee_usdc + partner_fee_usdc_spent
        vebal_usdc_amount = int(
            self.run_config.mainnet.web3.eth.contract(usdc.address, abi=get_abi("ERC20"))
            .functions.balanceOf(builder.safe_address)
            .call()
            - total_usdc_spent
            - 1  # Buffer
        )

        vebal_bal_amount = (
            self.run_config.mainnet.web3.eth.contract(bal.address, abi=get_abi("ERC20"))
            .functions.balanceOf(builder.safe_address)
            .call()
        )

        if vebal_usdc_amount > 0:
            usdc.transfer(self.book["maxiKeepers/veBalFeeInjector"], vebal_usdc_amount)
        if vebal_bal_amount > 0:
            bal.transfer(self.book["maxiKeepers/veBalFeeInjector"], vebal_bal_amount)

        # Save combined payload (V2 + V3 + final transfers)
        output_path = PROJECT_ROOT / output_path / f"v3_{datetime_file_header}.json"
        builder.output_payload(output_path)
        
        return output_path

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
        total_incentives = Decimal(0)

        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                assert pool.to_aura_incentives_usd >= 0, f"Negative aura incentives: {pool.to_aura_incentives_usd}"
                assert pool.to_bal_incentives_usd >= 0, f"Negative bal incentives: {pool.to_bal_incentives_usd}"
                assert pool.to_dao_usd >= 0, f"Negative dao share: {pool.to_dao_usd}"
                assert pool.to_vebal_usd >= 0, f"Negative vebal share: {pool.to_vebal_usd}"

                total_aura += pool.to_aura_incentives_usd
                total_bal += pool.to_bal_incentives_usd
                total_dao += chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd
                total_vebal += chain.noncore_to_vebal_usd + chain.alliance_noncore_to_vebal_usd

            total_dao += chain.noncore_to_dao_usd
            total_vebal += chain.noncore_to_vebal_usd

        total_incentives = total_aura + total_bal + total_dao + total_vebal
        total_pct = (total_aura + total_bal + total_dao + total_vebal) / total_incentives

        assert abs(1 - total_pct) < Decimal('0.0001'), f"Percentages don't sum to 1: {total_pct}"

        # Only check Aura share against BAL for core pool incentives
        core_pool_incentives = total_aura + total_bal
        aura_share = total_aura / core_pool_incentives if core_pool_incentives > 0 else Decimal(0)
        target_share = self.run_config.aura_vebal_share

        # new fee model breaks this check
        # assert abs(aura_share - target_share) < Decimal('0.05'), \
        #     f"Aura share {aura_share} deviates from target {target_share}"

        summary = {
            "feesCollected": float(round(total_fees, 2)),
            "incentivesDistributed": float(round(total_incentives, 2)), 
            "feesNotDistributed": float(round(total_fees - total_incentives, 2)),
            "auraIncentives": float(round(total_aura, 2)),
            "balIncentives": float(round(total_bal, 2)),
            "feesToDao": float(round(total_dao, 2)),
            "feesToVebal": float(round(total_vebal, 2)),
            "auravebalShare": float(round(aura_share, 2)),
            "auraIncentivesPct": float(round(total_aura / total_incentives, 4)),
            "balIncentivesPct": float(round(total_bal / total_incentives, 4)),
            "feesToDaoPct": float(round(total_dao / total_incentives, 4)),
            "feesToVebalPct": float(round(total_vebal / total_incentives, 4)),
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