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
from fee_allocator.accounting import PROJECT_ROOT
from fee_allocator.utils import get_hh_aura_target
from fee_allocator.logger import logger


class InputFees(TypedDict):
    chainname: int


base_dir = Path(__file__).parent


class FeeAllocator:
    def __init__(self, input_fees: InputFees, date_range: DateRange):
        self.chains = None
        self.input_fees = input_fees
        self.date_range = date_range

    def _get_chain_data(self, input_fees: InputFees, date_range: DateRange) -> Chains:
        if not isinstance(input_fees, dict):
            raise TypeError("invalid input fees")

        return Chains(
            [Chain(chain, fees, date_range) for chain, fees in input_fees.items()],
            date_range,
        )

    def generate_bribe_csv(self):
        if not self.chains:
            self.chains = self._get_chain_data(self.input_fees, self.date_range)

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
                        "amount": core_pool.redistributed.to_bal_incentives_usd,
                    },
                )
                output.append(
                    {
                        "target": core_pool.gauge_address,
                        "platform": "aura",
                        "amount": core_pool.redistributed.to_aura_incentives_usd,
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
        filename = (
            f"fee_allocator/allocations/output_for_msig/{datetime_file_header}.csv"
        )

        logger.info(f"Total fees collected: {self.chains.total_fees_collected_usd}")
        logger.info(
            f"Total incentives allocated: {self.chains.total_to_incentives_usd}"
        )
        logger.info(
            f"delta {self.chains.total_fees_collected_usd - self.chains.total_to_incentives_usd}"
        )

        df.to_csv(
            os.path.join(
                PROJECT_ROOT,
                filename,
            ),
            index=False,
        )
        return filename

    def generate_incentives_csv(self):
        if not self.chains:
            self.chains = self._get_chain_data(self.input_fees, self.date_range)

        logger.info("generating incentives csv")
        output = []
        for chain in self.chains.all_chains:
            for core_pool in chain.core_pools:
                if core_pool.total_to_incentives_usd == Decimal(0):
                    continue
                output.append(
                    {
                        "pool_id": core_pool.pool_id,
                        "chain": chain.name,
                        "symbol": core_pool.label,
                        "earned_fees": core_pool.total_earned_fees_usd,
                        "fees_to_vebal": core_pool.to_vebal_usd,
                        "fees_to_dao": core_pool.to_dao_usd,
                        "total_incentives": core_pool.total_to_incentives_usd,
                        "aura_incentives": core_pool.redistributed.to_aura_incentives_usd,
                        "bal_incentives": core_pool.redistributed.to_bal_incentives_usd,
                        "redirected_incentives": sum(
                            core_pool.redistributed.base_incentives
                        )
                        - core_pool.total_to_incentives_usd,
                        "reroute_incentives": 0,
                        "last_join_exit": core_pool.last_join_exit_ts,
                    },
                )

        df = pd.DataFrame(output)
        datetime_file_header = datetime.datetime.fromtimestamp(
            self.date_range[1]
        ).date()
        filename = f"fee_allocator/allocations/incentives/{datetime_file_header}.csv"
        sorted_df = df.sort_values(by=["chain", "earned_fees"], ascending=False)

        sorted_df.to_csv(
            os.path.join(
                PROJECT_ROOT,
                filename,
            ),
            index=False,
        )

    def generate_payload(self, csv_file_name: str):
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

        df = pd.read_csv(csv_file_name)
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

        builder.output_payload(f"fee_allocator/payloads/{datetime_file_header}.json")

    @staticmethod
    def _get_prop_hash(platform: str, target: str) -> str:
        if platform == "balancer":
            prop = Web3.solidity_keccak(["address"], [Web3.to_checksum_address(target)])
            return f"0x{prop.hex().lstrip('0x')}"
        if platform == "aura":
            return get_hh_aura_target(target)
        raise ValueError(f"platform {platform} not supported")
