import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv

from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.utils import get_last_thursday_odd_week


parser = argparse.ArgumentParser()
parser.add_argument("--ts_now", help="Current timestamp", type=int, required=False)
parser.add_argument(
    "--ts_in_the_past", help="Timestamp in the past", type=int, required=False
)
parser.add_argument(
    "--output_file_name", help="Output file name", type=str, required=False
)
parser.add_argument("--fees_file_name", help="Fees file name", type=str, required=False)

ROOT = os.path.dirname(__file__)

now = datetime.utcnow()
DELTA = 6000
TS_NOW = int(now.timestamp()) - DELTA
TS_2_WEEKS_AGO = int(get_last_thursday_odd_week().timestamp())


def main() -> None:
    load_dotenv()
    ts_now = parser.parse_args().ts_now or TS_NOW
    ts_in_the_past = parser.parse_args().ts_in_the_past or TS_2_WEEKS_AGO
    print(
        f"\n\n\n------\nRunning  from timestamps {ts_in_the_past} to {ts_now}\n------\n\n\n"
    )
    fees_file_name = parser.parse_args().fees_file_name or "current_fees_collected.json"
    input_fees_path = f"fee_allocator/fees_collected/{fees_file_name}"

    with open(input_fees_path) as f:
        input_fees = json.load(f)
        
    date_range = (ts_in_the_past, ts_now)

    fee_allocator = FeeAllocator(input_fees, date_range)

    fee_allocator.run_config.set_core_pool_chains_data()
    fee_allocator.run_config.set_aura_vebal_share()
    fee_allocator.run_config.set_initial_pool_allocation()

    fee_allocator.redistribute_fees()

    fee_allocator.generate_incentives_csv()
    file_name = fee_allocator.generate_bribe_csv()
    fee_allocator.generate_bribe_payload(file_name)


if __name__ == "__main__":
    main()
