import argparse
import os

from dotenv import load_dotenv

from pathlib import Path
from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.utils import fetch_collected_fees, parse_date_inputs
from fee_allocator.payload_visualizer import visualize_payload


parser = argparse.ArgumentParser()
parser.add_argument("--ts_now", help="Current timestamp", type=int, required=False)
parser.add_argument(
    "--ts_in_the_past", help="Timestamp in the past", type=int, required=False
)
parser.add_argument(
    "--date_range_string", help="Date range string in format YYYY-MM-DD_YYYY-MM-DD", type=str, required=False
)
parser.add_argument(
    "--output_file_name", help="Output file name", type=str, required=False
)
parser.add_argument("--fees_file_name", help="Fees file name", type=str, required=False)
parser.add_argument("--protocol_version", help="Protocol version (v2 or v3)", type=str, choices=["v2", "v3"], default="v2")
parser.add_argument("--no_visualize", help="Skip payload visualization", action="store_true", default=False)
parser.add_argument("--no_cache", help="Disable caching", action="store_true", default=False)

ROOT = os.path.dirname(__file__)


def main() -> None:
    load_dotenv()
    args = parser.parse_args()
    
    # Parse date inputs using utility function
    ts_in_the_past, ts_now, start_date, end_date = parse_date_inputs(
        args.date_range_string, args.ts_now, args.ts_in_the_past
    )
    
    # If date_range_string is provided, auto-construct fee file name if not explicitly provided
    if args.date_range_string and not args.fees_file_name:
        args.fees_file_name = f"{args.protocol_version}_fees_{start_date}_{end_date}.json"
    
    print(
        f"\n\n\n------\nRunning {args.protocol_version} allocation from timestamps {ts_in_the_past} to {ts_now}\n------\n\n\n"
    )

    input_fees = fetch_collected_fees(start_date, end_date, args.fees_file_name, args.protocol_version)
    date_range = (ts_in_the_past, ts_now)

    fee_allocator = FeeAllocator(input_fees, date_range, protocol_version=args.protocol_version, use_cache=not args.no_cache)

    fee_allocator.allocate()
    fee_allocator.recon()

    # Generate all artifacts
    artifacts = fee_allocator.generate_artifacts()
    payload_path = artifacts["payload"]

    fee_file_name = args.fees_file_name or f"{args.protocol_version}_fees_{start_date}_{end_date}.json"
    fee_file_path = Path(f"fee_allocator/fees_collected/{fee_file_name}")
    report_path = fee_allocator.generate_report(payload_path, [fee_file_path] if fee_file_path.exists() else None)

    if not args.no_visualize:
        print("\n" + "="*80 + "\n")
        visualize_payload(payload_path, [fee_file_path] if fee_file_path.exists() else None)
        print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()
