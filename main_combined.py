import argparse
import os
from datetime import datetime
from pathlib import Path
import pytz

from dotenv import load_dotenv

from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.utils import fetch_collected_fees, parse_date_inputs
from fee_allocator.payload_visualizer import visualize_combined_payload, save_combined_report
from combine_payloads import combine_payloads_from_paths


parser = argparse.ArgumentParser()
parser.add_argument("--ts_now", help="Current timestamp", type=int, required=False)
parser.add_argument(
    "--ts_in_the_past", help="Timestamp in the past", type=int, required=False
)
parser.add_argument(
    "--date_range_string", help="Date range string in format YYYY-MM-DD_YYYY-MM-DD", type=str, required=False
)
parser.add_argument(
    "--v2_fees_file_name", help="V2 fees file name", type=str, required=False
)
parser.add_argument(
    "--v3_fees_file_name", help="V3 fees file name", type=str, required=False
)
parser.add_argument("--no_visualize", help="Skip payload visualization", action="store_true", default=False)

ROOT = os.path.dirname(__file__)


def main() -> None:
    load_dotenv()
    args = parser.parse_args()
    
    ts_in_the_past, ts_now, start_date, end_date = parse_date_inputs(
        args.date_range_string, args.ts_now, args.ts_in_the_past
    )

    # If date_range_string is provided, auto-construct fee file names if not explicitly provided
    if args.date_range_string:
        if not args.v2_fees_file_name:
            args.v2_fees_file_name = f"v2_fees_{start_date}_{end_date}.json"
        if not args.v3_fees_file_name:
            args.v3_fees_file_name = f"v3_fees_{start_date}_{end_date}.json"
    
    print(f"\n\n\n------\nRunning combined V2+V3 allocation from timestamps {ts_in_the_past} to {ts_now}\n------\n\n\n")
    
    date_range = (ts_in_the_past, ts_now)
    
    print("\n=== Running V2 Allocation ===\n")
    v2_input_fees = fetch_collected_fees(start_date, end_date, args.v2_fees_file_name, "v2")
    v2_allocator = FeeAllocator(v2_input_fees, date_range, protocol_version="v2")
    
    v2_allocator.allocate()
    v2_allocator.recon()
    
    v2_artifacts = v2_allocator.generate_artifacts(include_bal_transfer=False)
    v2_payload_path = v2_artifacts["payload"]
    
    print("\n=== Running V3 Allocation ===\n")
    v3_input_fees = fetch_collected_fees(start_date, end_date, args.v3_fees_file_name, "v3")
    v3_allocator = FeeAllocator(v3_input_fees, date_range, protocol_version="v3")
    
    v3_allocator.allocate()
    v3_allocator.recon()

    v3_artifacts = v3_allocator.generate_artifacts(include_bal_transfer=True)
    v3_payload_path = v3_artifacts["payload"]
    
    print("\n=== Combining V2 and V3 Payloads ===\n")
    date_str = datetime.fromtimestamp(ts_now, tz=pytz.UTC).strftime("%Y-%m-%d")
    combined_output_path = Path(ROOT) / "fee_allocator" / "payloads" / f"{date_str}.json"
    combined_payload_path = combine_payloads_from_paths(v2_payload_path, v3_payload_path, combined_output_path)
    
    print("\n=== Combined allocation complete! ===\n")
    
    v2_fee_file_name = args.v2_fees_file_name or f"v2_fees_{start_date}_{end_date}.json"
    v3_fee_file_name = args.v3_fees_file_name or f"v3_fees_{start_date}_{end_date}.json"
    v2_fee_file_path = Path(f"fee_allocator/fees_collected/{v2_fee_file_name}")
    v3_fee_file_path = Path(f"fee_allocator/fees_collected/{v3_fee_file_name}")

    if not args.no_visualize:
        print("\n" + "="*80 + "\n")
        visualize_combined_payload(combined_payload_path, v2_fee_file_path, v3_fee_file_path)
        print("\n" + "="*80 + "\n")

    save_combined_report(combined_payload_path, v2_fee_file_path, v3_fee_file_path)


if __name__ == "__main__":
    main()