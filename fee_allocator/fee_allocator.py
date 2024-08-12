from typing import TypedDict
from bal_tools.subgraph import DateRange
from bal_tools.safe_tx_builder import SafeTxBuilder, SafeContract
from bal_tools.utils import get_abi
import pandas as pd
from decimal import Decimal
import os
import datetime
from pathlib import Path
from web3 import Web3

from fee_allocator.accounting.chains import Chain, Chains
from fee_allocator.accounting.core_pools import CorePool
from fee_allocator.accounting import PROJECT_ROOT
from fee_allocator.utils import get_hh_aura_target
from fee_allocator.logger import logger


class InputFees(TypedDict):
    chainname: int


base_dir = Path(__file__).parent


class FeeAllocator:
    def __init__(
        self,
        input_fees: InputFees,
        date_range: DateRange,
        cache_dir: Path = None,
        use_cache: bool = True,
    ):
        self.chains = self._get_chain_data(input_fees, date_range, cache_dir, use_cache)
        self.input_fees = input_fees
        self.date_range = date_range

    def _get_chain_data(
        self,
        input_fees: InputFees,
        date_range: DateRange,
        cache_dir: Path,
        use_cache: bool,
    ) -> Chains:
        return Chains(
            [Chain(chain, fees, date_range) for chain, fees in input_fees.items()],
            date_range,
            cache_dir=cache_dir,
            use_cache=use_cache,
        )

    def redistribute_fees(self):
        for chain in self.chains.all_chains:
            pools_to_redistribute = [
                pool
                for pool in chain.core_pools
                if pool.total_to_incentives_usd
                < chain.fee_config.min_vote_incentive_amount
            ]
            pools_to_receive = [
                pool
                for pool in chain.core_pools
                if pool.total_to_incentives_usd
                > chain.fee_config.min_vote_incentive_amount
            ]

            for pool_to_redist in pools_to_redistribute:
                total_to_incentives = pool_to_redist.total_to_incentives_usd
                to_aura_incentives = pool_to_redist.to_aura_incentives_usd
                to_bal_incentives = pool_to_redist.to_bal_incentives_usd

                pool_to_redist.total_to_incentives_usd = Decimal(0)
                pool_to_redist.to_aura_incentives_usd = Decimal(0)
                pool_to_redist.to_bal_incentives_usd = Decimal(0)
                pool_to_redist.redirected_incentives_usd -= total_to_incentives

                pool_weights = {
                    pool.pool_id: pool.total_earned_fees_usd
                    / sum([pool.total_earned_fees_usd for pool in pools_to_receive])
                    for pool in pools_to_receive
                }

                for pool_to_receive in pools_to_receive:
                    pool_weight = pool_weights[pool_to_receive.pool_id]
                    pool_to_receive.total_to_incentives_usd += (
                        total_to_incentives * pool_weight
                    )
                    pool_to_receive.to_aura_incentives_usd += (
                        to_aura_incentives * pool_weight
                    )
                    pool_to_receive.to_bal_incentives_usd += (
                        to_bal_incentives * pool_weight
                    )
                    pool_to_receive.redirected_incentives_usd += (
                        total_to_incentives * pool_weight
                    )

        self.handle_aura_min(buffer=0.25)
        self.handle_aura_min()

    def handle_aura_min(self, buffer=0):
        for chain in self.chains.all_chains:
            min_aura_incentive = chain.fee_config.min_aura_incentive * (1 - buffer)
            debt_to_aura_market = Decimal(0)
            for core_pool in chain.core_pools:
                override_aura_to_bal = (
                    core_pool.override and core_pool.override.voting_pool == "bal"
                )
                if (
                    core_pool.to_aura_incentives_usd < min_aura_incentive
                    or override_aura_to_bal
                ):
                    to_redistribute = core_pool.to_aura_incentives_usd
                    core_pool.to_aura_incentives_usd = Decimal(0)
                    core_pool.to_bal_incentives_usd += to_redistribute
                    debt_to_aura_market += to_redistribute

            if debt_to_aura_market:
                debt_repaid = Decimal(0)
                pools_over_aura_min = [
                    pool
                    for pool in chain.core_pools
                    if pool.to_aura_incentives_usd >= min_aura_incentive
                ]
                if pools_over_aura_min:
                    amount_per_pool = debt_to_aura_market / len(pools_over_aura_min)
                    for pool in pools_over_aura_min:
                        amount_to_dist = min(
                            amount_per_pool, pool.to_bal_incentives_usd
                        )
                        pool.to_aura_incentives_usd += amount_to_dist
                        pool.to_bal_incentives_usd -= amount_to_dist
                        debt_repaid += amount_to_dist

                        if debt_to_aura_market - debt_repaid >= 0:
                            print(
                                f"{pool.pool_id}  remaining debt to aura market: {debt_to_aura_market}, Debt repaid: {debt_repaid}, debt remaining: {debt_to_aura_market - debt_repaid}"
                            )

    def generate_bribe_csv(
        self, output_path: Path = Path("fee_allocator/allocations/output_for_msig")
    ) -> Path:
        logger.info("generating bribe csv")
        output = []
        for chain in self.chains.all_chains:
            for core_pool in chain.core_pools:
                if core_pool.total_to_incentives_usd == Decimal(0):
                    continue

                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "balancer",
                        "amount": core_pool.to_bal_incentives_usd,
                    },
                )
                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "aura",
                        "amount": core_pool.to_aura_incentives_usd,
                    },
                )

        output.append(
            {
                "target": "0x10A19e7eE7d7F8a52822f6817de8ea18204F2e4f",  # DAO msig
                "platform": "payment",
                "amount": self.chains.total_to_dao_usd,
            }
        )

        df = pd.DataFrame(output)
        datetime_file_header = datetime.datetime.fromtimestamp(
            self.date_range[1]
        ).date()
        output_path = PROJECT_ROOT / output_path / f"bribes_{datetime_file_header}.csv"
        output_path.parent.mkdir(exist_ok=True)

        logger.info(f"Total fees collected: {self.chains.total_fees_collected_usd}")
        logger.info(
            f"Total incentives allocated: {self.chains.total_to_incentives_usd}"
        )
        logger.info(
            f"delta {self.chains.total_fees_collected_usd - self.chains.total_to_incentives_usd}"
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
        for chain in self.chains.all_chains:
            for core_pool in chain.core_pools:
                if not any(
                    [
                        core_pool.total_to_incentives_usd,
                        core_pool.to_aura_incentives_usd,
                        core_pool.to_bal_incentives_usd,
                    ]
                ):
                    continue
                output.append(
                    {
                        "pool_id": core_pool.pool_id,
                        "chain": chain.name,
                        "symbol": core_pool.label,
                        "earned_fees": round(core_pool.total_earned_fees_usd, 4),
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
                    },
                )

        df = pd.DataFrame(output)
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

    def generate_payload(
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

        web3 = (
            self.chains.mainnet.web3
            if self.chains
            else Web3(Web3.HTTPProvider(os.environ["MAINNETNODEURL"]))
        )
        vebal_usdc_amount = (
            web3.eth.contract(usdc.address, abi=get_abi("ERC20"))
            .functions.balanceOf(builder.safe_address)
            .call()
            - sum(df["amount"])
            - 1
        )
        vebal_bal_amount = (
            web3.eth.contract(bal.address, abi=get_abi("ERC20"))
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
