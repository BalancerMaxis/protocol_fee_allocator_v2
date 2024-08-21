import pandas as pd
from pathlib import Path
from dataclasses import dataclass
import json
import datetime


@dataclass
class V1FeeAllocation:
    input_fees: dict
    allocations: pd.DataFrame
    start_date: datetime.datetime
    end_date: datetime.datetime
    start_ts: int = 0
    end_ts: int = 0

    def __post_init__(self):
        self.start_ts = int(
            self.start_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
        )
        self.end_ts = int(
            self.end_date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )


def is_wei(fee_amt):
    return len(str(int(fee_amt))) > 8


def extract_dates(filename):
    parts = filename.stem.split("_")
    start_date = datetime.datetime.strptime(parts[1], "%Y-%m-%d")
    end_date = datetime.datetime.strptime(parts[2], "%Y-%m-%d")
    return start_date, end_date


def gather_v1_allocations():
    fee_allocations: list[V1FeeAllocation] = []
    v1_data = Path("backtesting/v1_allocations")
    v1_allocations = sorted(
        v1_data.joinpath("allocations").glob("*.csv"),
        key=lambda x: x.stem.split("_")[1],
    )
    v1_fees = sorted(
        v1_data.joinpath("fees_collected").glob("*.json"),
        key=lambda x: x.stem.split("_")[1],
    )

    for allocation_file, fee_file in zip(v1_allocations, v1_fees):
        allocation_start, allocation_end = extract_dates(allocation_file)
        fee_start, fee_end = extract_dates(fee_file)
        assert (
            allocation_start == fee_start and allocation_end == fee_end
        ), f"Mismatched dates: {allocation_start}-{allocation_end} != {fee_start}-{fee_end}"

        input_fees = {
            k: v / 1e6 if is_wei(v) else v
            for k, v in json.load(open(fee_file, "r")).items()
            if k != "zkevm"
        }
        allocations = pd.read_csv(allocation_file)

        fee_allocations.append(
            V1FeeAllocation(input_fees, allocations, fee_start, fee_end)
        )

    return fee_allocations
