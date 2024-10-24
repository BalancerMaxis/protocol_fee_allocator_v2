from typing import TypedDict, Union
from bal_tools.subgraph import DateRange
from bal_tools.safe_tx_builder import SafeTxBuilder, SafeContract
from bal_tools.utils import get_abi
import pandas as pd
from decimal import Decimal
import datetime
from pathlib import Path
from web3 import Web3
from dotenv import load_dotenv

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
        self.run_config = CorePoolRunConfig(self.input_fees, self.date_range, cache_dir, use_cache)


    def redistribute_fees(self):
        """
        Redistributes fees among pools based on minimum incentive amounts and chain-specific rules.
        This method performs the following steps:
        1. Identifies pools with incentives below the minimum threshold.
        2. Redistributes fees from these pools to eligible pools above the threshold.
        3. Recalculates incentive amounts for Aura and Balancer.
        4. Adjusts DAO and veBAL shares based on the new distribution.
        5. Handles Aura minimum incentives with and without a buffer.
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
                pool.to_aura_incentives_usd += total * self.run_config.aura_vebal_share
                pool.to_bal_incentives_usd += total * (1 - self.run_config.aura_vebal_share)

            total_to_incentives = sum(p.total_to_incentives_usd for p in chain.core_pools)
            for pool in chain.core_pools:
                if total_to_incentives > 0:
                    pool.earned_fee_share_of_chain_usd = pool.total_to_incentives_usd / total_to_incentives
                    pool.to_dao_usd = pool.earned_fee_share_of_chain_usd * chain.fees_collected * self.run_config.fee_config.dao_share_pct
                    pool.to_vebal_usd = pool.earned_fee_share_of_chain_usd * chain.fees_collected * self.run_config.fee_config.vebal_share_pct
                else:
                    pool.earned_fee_share_of_chain_usd = pool.to_dao_usd = pool.to_vebal_usd = Decimal(0)

        self._handle_aura_min(buffer=0.25)
        self._handle_aura_min()

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

        output.append(
            {
                "target": "0x10A19e7eE7d7F8a52822f6817de8ea18204F2e4f",  # DAO msig
                "platform": "payment",
                "amount": self.run_config.total_to_dao_usd,
            }
        )

        df = pd.DataFrame(output)
        datetime_file_header = datetime.datetime.fromtimestamp(
            self.date_range[1]
        ).date()
        output_path = PROJECT_ROOT / output_path / f"bribes_{datetime_file_header}.csv"
        output_path.parent.mkdir(exist_ok=True)

        logger.info(f"Total fees collected: {self.run_config.total_fees_collected_usd}")
        logger.info(
            f"Total incentives allocated: {self.run_config.total_to_incentives_usd}"
        )
        logger.info(
            f"delta {self.run_config.total_fees_collected_usd - self.run_config.total_to_incentives_usd}"
        )

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
                        "last_join_exit": core_pool.last_join_exit_ts,
                        "bpt_price": round(core_pool.bpt_price, 4),
                        "earned_fees": round(core_pool.total_earned_fees_usd_twap, 4),
                        "total_distribtuion": round(sum([core_pool.to_vebal_usd, core_pool.to_dao_usd, core_pool.total_to_incentives_usd]), 4),
                        "fees_to_vebal": round(core_pool.to_vebal_usd, 4),
                        "fees_to_dao": round(core_pool.to_dao_usd, 4),
                        "total_incentives": round(core_pool.total_to_incentives_usd, 4),
                        "aura_incentives": round(core_pool.to_aura_incentives_usd, 4),
                        "bal_incentives": round(core_pool.to_bal_incentives_usd, 4),
                        "redirected_incentives": round(
                            core_pool.redirected_incentives_usd, 4
                        ),
                        "reroute_incentives": 0,
                    },
                )

        df = pd.DataFrame(output)
        
        logger.info(f"Total fees collected: {self.run_config.total_fees_collected_usd}")
        logger.info(
            f"Total incentives allocated: {self.run_config.total_to_incentives_usd}"
        )
        logger.info(
            f"delta {self.run_config.total_fees_collected_usd - self.run_config.total_to_incentives_usd}"
        )
        
    
        sorted_df = df.sort_values(by=["chain", "earned_fees"], ascending=False)
        start_date = datetime.datetime.fromtimestamp(self.date_range[0]).date()
        end_date = datetime.datetime.fromtimestamp(self.date_range[1]).date()
        output_path = (
            PROJECT_ROOT / output_path / f"incentives_{start_date}_{end_date}.csv"
        )
        output_path.parent.mkdir(exist_ok=True)

        sorted_df.to_csv(
            output_path,
            index=False,
        )

        return output_path

    def generate_bribe_payload(
        self, input_csv: str, output_path: Path = Path("fee_allocator/payloads")
    ) -> Path:
        """
        builds a safe payload from the bribe csv
        """
        logger.info("generating payload")
        builder = SafeTxBuilder("multisigs/fees")
        usdc = SafeContract("tokens/USDC", abi_file_path=f"{base_dir}/abi/ERC20.json")
        bal = SafeContract("tokens/BAL", abi_file_path=f"{base_dir}/abi/ERC20.json")
        aura_bribe_market = SafeContract(
            "hidden_hand2/aura_briber",
            abi_file_path=f"{base_dir}/abi/bribe_market.json",
        )
        bal_bribe_market = SafeContract(
            "hidden_hand2/balancer_briber",
            abi_file_path=f"{base_dir}/abi/bribe_market.json",
        )

        df = pd.read_csv(input_csv)
        bribe_df = df[df["platform"].isin(["balancer", "aura"])]
        payment_df = df[df["platform"] == "payment"].iloc[0]

        total_bribe_usdc = sum(bribe_df["amount"]) * 1e6

        """
        bribe txs
        """
        usdc.approve("hidden_hand2/bribe_vault", total_bribe_usdc)

        for _, row in bribe_df.iterrows():
            prop_hash = self._get_prop_hash(row["platform"], row["target"])
            mantissa = int(row["amount"] * 1e6)

            if row["platform"] == "balancer":
                bal_bribe_market.depositBribe(prop_hash, "tokens/USDC", mantissa, 0, 2)
            elif row["platform"] == "aura":
                aura_bribe_market.depositBribe(prop_hash, "tokens/USDC", mantissa, 0, 1)

        vebal_usdc_amount = (
            self.run_config.mainnet.web3.eth.contract(usdc.address, abi=get_abi("ERC20"))
            .functions.balanceOf(builder.safe_address)
            .call()
            - sum(df["amount"])
            - 1
        )
        vebal_bal_amount = (
            self.run_config.mainnet.web3.eth.contract(bal.address, abi=get_abi("ERC20"))
            .functions.balanceOf(builder.safe_address)
            .call()
        )

        """
        transfer txs
        """
        usdc.transfer(payment_df["target"], payment_df["amount"])
        usdc.transfer("maxiKeepers/veBalFeeInjector", vebal_usdc_amount * 1e6)
        bal.transfer("maxiKeepers/veBalFeeInjector", vebal_bal_amount * 1e18)

        datetime_file_header = datetime.datetime.fromtimestamp(
            self.date_range[1]
        ).date()

        output_path = PROJECT_ROOT / output_path / f"{datetime_file_header}.json"
        output_path.parent.mkdir(exist_ok=True)
        builder.output_payload(output_path)

        return output_path

    @staticmethod
    def _get_prop_hash(platform: str, target: str) -> str:
        if platform == "balancer":
            prop = Web3.solidity_keccak(["address"], [Web3.to_checksum_address(target)])
            return f"0x{prop.hex().lstrip('0x')}"
        if platform == "aura":
            return get_hh_aura_target(target)
        raise ValueError(f"platform {platform} not supported")
