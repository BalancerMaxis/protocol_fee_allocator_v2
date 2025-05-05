import argparse
import os
from datetime import datetime
import pytz

from dotenv import load_dotenv

from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.utils import get_last_thursday_odd_week, fetch_collected_fees


parser = argparse.ArgumentParser()
parser.add_argument("--ts_now", help="Current timestamp", type=int, required=False)
parser.add_argument(
    "--ts_in_the_past", help="Timestamp in the past", type=int, required=False
)
parser.add_argument(
    "--output_file_name", help="Output file name", type=str, required=False
)
parser.add_argument("--fees_file_name", help="Fees file name", type=str, required=False)
parser.add_argument("--protocol_version", help="Protocol version (v2 or v3)", type=str, choices=["v2", "v3"], default="v2")

ROOT = os.path.dirname(__file__)

now = datetime.utcnow()
DELTA = 6000
TS_NOW = int(now.timestamp()) - DELTA
TS_2_WEEKS_AGO = int(get_last_thursday_odd_week().timestamp())


def main() -> None:
    load_dotenv()
    args = parser.parse_args()
    ts_now = args.ts_now or TS_NOW
    ts_in_the_past = args.ts_in_the_past or TS_2_WEEKS_AGO
    print(
        f"\n\n\n------\nRunning  from timestamps {ts_in_the_past} to {ts_now}\n------\n\n\n"
    )
    
    start_date = datetime.fromtimestamp(ts_in_the_past, tz=pytz.UTC).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(ts_now, tz=pytz.UTC).strftime("%Y-%m-%d")

    input_fees = fetch_collected_fees(start_date, end_date, args.fees_file_name, args.protocol_version)
    date_range = (ts_in_the_past, ts_now)

    fee_allocator = FeeAllocator(input_fees, date_range, protocol_version=args.protocol_version)

    fee_allocator.allocate()
    fee_allocator.recon()

    fee_allocator.generate_incentives_csv()
    file_name = fee_allocator.generate_bribe_csv()
    fee_allocator.generate_bribe_payload(file_name)
    fee_allocator.generate_noncore_csv()


if __name__ == "__main__":
    main()
