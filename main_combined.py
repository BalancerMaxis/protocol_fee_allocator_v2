import argparse
import os
from datetime import datetime
from pathlib import Path
import pytz

from dotenv import load_dotenv

from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.utils import get_last_thursday_odd_week, fetch_collected_fees
from fee_allocator.payload_visualizer import visualize_combined_payload
from combine_payloads import combine_payloads_from_paths


parser = argparse.ArgumentParser()
parser.add_argument("--ts_now", help="Current timestamp", type=int, required=False)
parser.add_argument(
    "--ts_in_the_past", help="Timestamp in the past", type=int, required=False
)
parser.add_argument(
    "--v2_fees_file_name", help="V2 fees file name", type=str, required=False
)
parser.add_argument(
    "--v3_fees_file_name", help="V3 fees file name", type=str, required=False
)
parser.add_argument("--no-visualize", help="Skip payload visualization", action="store_true", default=False)

ROOT = os.path.dirname(__file__)

now = datetime.now(pytz.UTC)
DELTA = 6000
TS_NOW = int(now.timestamp()) - DELTA
TS_2_WEEKS_AGO = int(get_last_thursday_odd_week().timestamp())




def main() -> None:
    load_dotenv()
    args = parser.parse_args()
    ts_now = args.ts_now or TS_NOW
    ts_in_the_past = args.ts_in_the_past or TS_2_WEEKS_AGO
    
    print(f"\n\n\n------\nRunning combined V2+V3 allocation from timestamps {ts_in_the_past} to {ts_now}\n------\n\n\n")
    
    start_date = datetime.fromtimestamp(ts_in_the_past, tz=pytz.UTC).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(ts_now, tz=pytz.UTC).strftime("%Y-%m-%d")
    
    date_range = (ts_in_the_past, ts_now)
    
    print("\n=== Running V2 Allocation ===\n")
    v2_input_fees = fetch_collected_fees(start_date, end_date, args.v2_fees_file_name, "v2")
    v2_allocator = FeeAllocator(v2_input_fees, date_range, protocol_version="v2")
    
    v2_allocator.allocate()
    v2_allocator.recon()
    
    v2_allocator.generate_incentives_csv()
    v2_bribe_file = v2_allocator.generate_bribe_csv()
    v2_partner_file = v2_allocator.generate_partner_csv()
    v2_payload_path = v2_allocator.generate_bribe_payload(
        v2_bribe_file, 
        partner_csv=v2_partner_file,
        include_bal_transfer=False  # Skip BAL transfer for v2 in combined mode
    )
    v2_allocator.generate_noncore_csv()
    
    print("\n=== Running V3 Allocation ===\n")
    v3_input_fees = fetch_collected_fees(start_date, end_date, args.v3_fees_file_name, "v3")
    v3_allocator = FeeAllocator(v3_input_fees, date_range, protocol_version="v3")
    
    v3_allocator.allocate()
    v3_allocator.recon()
    
    v3_allocator.generate_incentives_csv()
    v3_bribe_file = v3_allocator.generate_bribe_csv()
    v3_partner_file = v3_allocator.generate_partner_csv()
    v3_payload_path = v3_allocator.generate_bribe_payload(
        v3_bribe_file, 
        partner_csv=v3_partner_file,
        include_bal_transfer=True  # Include BAL transfer for v3 in combined mode
    )
    v3_allocator.generate_noncore_csv()
    
    print("\n=== Combining V2 and V3 Payloads ===\n")
    date_str = datetime.fromtimestamp(ts_now, tz=pytz.UTC).strftime("%Y-%m-%d")
    combined_output_path = Path(ROOT) / "fee_allocator" / "payloads" / f"{date_str}.json"
    combined_payload_path = combine_payloads_from_paths(v2_payload_path, v3_payload_path, combined_output_path)
    
    print("\n=== Combined allocation complete! ===\n")
    
    if not args.no_visualize:
        print("\n" + "="*80 + "\n")
        v2_fee_file_name = args.v2_fees_file_name or f"v2_fees_{start_date}_{end_date}.json"
        v3_fee_file_name = args.v3_fees_file_name or f"v3_fees_{start_date}_{end_date}.json"
        v2_fee_file_path = Path(f"fee_allocator/fees_collected/{v2_fee_file_name}")
        v3_fee_file_path = Path(f"fee_allocator/fees_collected/{v3_fee_file_name}")
        
        visualize_combined_payload(combined_payload_path, v2_fee_file_path, v3_fee_file_path)
        print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()