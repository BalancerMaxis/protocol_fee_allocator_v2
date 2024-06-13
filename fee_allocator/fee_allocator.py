from typing import TypedDict, NewType
from bal_tools.subgraph import DateRange
import pandas as pd
from decimal import Decimal
import os
import datetime

from fee_allocator.accounting.chains import Chain, Chains
from fee_allocator.accounting import PROJECT_ROOT


class InputFees(TypedDict):
    chainname: int


class FeeAllocator:
    def __init__(self, input_fees: InputFees, date_range: DateRange):
        self.chains = self._load_input_fees(input_fees, date_range)
        self.date_range = date_range

    def _load_input_fees(self, input_fees: InputFees, date_range: DateRange):
        if not isinstance(input_fees, dict):
            raise TypeError("invalid input fees")

        return Chains(
            [Chain(chain, fees, date_range) for chain, fees in input_fees.items()],
            date_range,
        )

    def generate_bribe_csv(self):
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
        filename = (
            f"fee_allocator/allocations/output_for_msig/{datetime_file_header}.csv"
        )

        print(f"Total fees collected: {self.chains.total_fees_collected_usd}")
        print(f"Total incentives collected: {self.chains.total_to_incentives_usd}")
        print(
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
